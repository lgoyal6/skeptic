"""
Tests for the replay corpus: the fixtures, and the negative controls on them.

The corpus exists so that claims about real tools are reproducible by someone
who is not sitting at the machine that made them. That only holds if the
fixtures still verify, if the checks that read them can return every verdict
they are supposed to be able to return, and if the machinery cannot be fooled
by the two accidents most likely to fake a contract change: a redaction, and a
reordered JSON object.

No network, no credentials, no model calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fixtures.checks import (
    CHECKS,
    CONTRADICTS,
    SUPPORTS,
    UNOBSERVABLE,
    evaluate,
    load_traffic,
)
from fixtures.format import (
    REDACTED,
    body_hash,
    call_key,
    canonical,
    fixture_dir,
    read_manifest,
    redact,
    tools,
    verify,
)

CORPUS = tools()
REQUIRED_CLASSES = {
    "status-code-mismatch", "field-omission", "renamed-field", "rate-limit",
    "pagination-cursor", "semantic-type-correct", "no-mismatch-control", "unobservable",
}


def _rules(tool: str, version: str) -> list[dict]:
    p = fixture_dir(tool, version) / "expected.yaml"
    return (yaml.safe_load(p.read_text()) or {}).get("rules") or []


def _all_rules():
    for t, v in CORPUS:
        for r in _rules(t, v):
            yield t, v, r


# ---------------------------------------------------------------------------
# The corpus itself
# ---------------------------------------------------------------------------


def test_corpus_covers_at_least_five_tools():
    """One tool's quirks are an anecdote. The claim is about a method, so the corpus has to span tools that share no code, no vendor and no docs style."""
    assert len({t for t, _ in CORPUS}) >= 5, f"only {len({t for t, _ in CORPUS})} tools"


def test_corpus_covers_every_required_mismatch_class():
    """A corpus that happens to contain six flavours of pagination bug would prove much less than its size suggests."""
    seen = {str(r.get("mismatch_class", "")) for _, _, r in _all_rules()}
    assert REQUIRED_CLASSES <= seen, f"missing classes: {sorted(REQUIRED_CLASSES - seen)}"


@pytest.mark.parametrize("tool,version", CORPUS, ids=[f"{t}@{v}" for t, v in CORPUS])
def test_every_fixture_verifies_against_its_manifest(tool, version):
    """Hashes, required manifest fields, and -- the one that matters -- that the normalized traffic can still be re-derived from the immutable raw capture."""
    assert verify(tool, version) == []


@pytest.mark.parametrize("tool,version", CORPUS, ids=[f"{t}@{v}" for t, v in CORPUS])
def test_manifest_records_the_redistribution_boundary(tool, version):
    """A corpus of somebody else's responses needs its boundary written down, including where it is only as-stated rather than verified."""
    lic = read_manifest(tool, version)["license"]
    assert lic.get("name") and lic.get("terms_url") and lic.get("redistribution")
    assert isinstance(lic.get("verified_from_terms_page"), bool)


@pytest.mark.parametrize("tool,version", CORPUS, ids=[f"{t}@{v}" for t, v in CORPUS])
def test_no_fixture_records_a_mutating_call_against_a_third_party(tool, version):
    """The capture driver refuses non-GET against a remote target. That rule has to survive the trip to disk, because a fixture is a file and files get edited."""
    m = read_manifest(tool, version)
    if not m["local_target"]:
        assert m["methods_used"] == ["GET"], f"{tool} records {m['methods_used']}"
        assert m["endpoint"]["base_url"].startswith("https://")


def test_every_rule_has_a_registered_check():
    """An answer key with no executable check is prose. It would sit in the corpus looking like coverage and assert nothing."""
    missing = [f"{t}@{v}:{r['id']}" for t, v, r in _all_rules() if r["id"] not in CHECKS]
    assert not missing, f"rules with no check: {missing}"


# ---------------------------------------------------------------------------
# Negative control 1: an honest fixture must not manufacture a mismatch
# ---------------------------------------------------------------------------


CONTROLS = [(t, v, r) for t, v, r in _all_rules()
            if r.get("mismatch_class") == "no-mismatch-control"]


@pytest.mark.parametrize(
    "tool,version,rule", CONTROLS,
    ids=[f"{t}:{r['id']}" for t, _v, r in CONTROLS])
def test_honest_behaviour_does_not_become_a_false_belief(tool, version, rule):
    """Six fixtures record a tool doing exactly what its documentation says.

    MediaWiki clamps `srlimit` to 500 and says so in the response; GitHub's
    rate-limit headers match its documented limit; PokeAPI's pagination reports
    its own end correctly. A checker eager enough to find a mismatch in every
    recording would find one here, and it would be false. These are the rules
    that make precision mean something.
    """
    v = evaluate(rule["id"], load_traffic(tool, version))
    assert v.outcome == SUPPORTS, f"{rule['id']}: {v.outcome} -- {v.detail}"


def test_the_same_clamp_is_a_mismatch_in_one_tool_and_not_in_another():
    """The sharpest control in the corpus: three APIs clamp a page-size parameter identically, and only the one that stays silent about it is a mismatch."""
    silent_lab = evaluate("page_size_cap", load_traffic("lab", "seed1337-2026-09-19"))
    silent_gh = evaluate("per_page_silently_clamped", load_traffic("github", "rest-2026-09-19"))
    announced = evaluate("clamp_is_announced_in_band",
                         load_traffic("wikipedia", "actionapi-2026-09-19"))
    assert silent_lab.outcome == CONTRADICTS
    assert silent_gh.outcome == CONTRADICTS
    assert announced.outcome == SUPPORTS, (
        "MediaWiki performs the same clamp and discloses it; calling that a mismatch "
        "would mean the checker is detecting clamping rather than dishonesty")


# ---------------------------------------------------------------------------
# Negative control 2: an unobservable claim must produce abstention
# ---------------------------------------------------------------------------


UNOBS = [(t, v, r) for t, v, r in _all_rules()
         if not r.get("observable", True)]


@pytest.mark.parametrize(
    "tool,version,rule", UNOBS,
    ids=[f"{t}:{r['id']}" for t, _v, r in UNOBS])
def test_unobservable_claims_abstain_rather_than_invent(tool, version, rule):
    """Two documented promises in this corpus cannot be checked from a recording.

    GitHub's `x-ratelimit-reset` names a moment an hour after capture, and no
    recording contains an observation of a later moment. REST Countries' field
    selection is unobservable for a different reason: every input produces
    byte-identical output, because the deprecation short-circuits before any
    field handling, so nothing distinguishes the cases.

    Both must return `unobservable`. A checker with only two verdicts available
    would have to pick one, and picking is how every vacuous-measurement bug in
    this project's history happened.
    """
    v = evaluate(rule["id"], load_traffic(tool, version))
    assert v.outcome == UNOBSERVABLE, f"{rule['id']}: {v.outcome} -- {v.detail}"


def test_abstention_is_reachable_and_not_merely_declared():
    """`UNOBSERVABLE` has to be a verdict the machinery actually returns, not a constant nothing produces.

    Asserted against string literals rather than against the module's own
    constants, and deliberately so. The first version of this test compared
    `outcomes` to `{CONTRADICTS, SUPPORTS, UNOBSERVABLE}`, which is vacuous:
    redefine `UNOBSERVABLE = "supports_doc"` and both sides collapse together
    and the test still passes. The mutation harness caught it -- the same
    measuring-without-establishing-observability bug this suite exists to
    guard against, in the guard itself.
    """
    outcomes = {evaluate(r["id"], load_traffic(t, v)).outcome for t, v, r in _all_rules()}
    assert outcomes == {"contradicts_doc", "supports_doc", "unobservable"}, (
        f"the corpus only ever produces {sorted(outcomes)}; a verdict no fixture "
        f"reaches is not evidence that the code can reach it")
    assert len({CONTRADICTS, SUPPORTS, UNOBSERVABLE}) == 3, (
        "the three verdicts must be distinct values; collapsing two of them would "
        "make every comparison in this file trivially true")


# ---------------------------------------------------------------------------
# Negative control 3: a redaction must not become a distinguishing signal
# ---------------------------------------------------------------------------


def test_redaction_writes_a_constant_so_it_carries_no_information():
    """Two different secrets must redact to the same bytes.

    A per-secret placeholder would leak exactly what it was meant to hide:
    two exchanges identical except for a stripped credential would still
    differ, and a drift detector comparing them would report a contract change
    caused by the redaction rather than by the tool.
    """
    a, fa = redact({"token": "Bearer abcdefghijklmnop", "n": 1})
    b, fb = redact({"token": "Bearer zyxwvutsrqponml", "n": 1})
    assert a == b, "two different secrets produced two different redactions"
    assert fa == fb == ["bearer_token"]
    assert REDACTED in json.dumps(a)
    assert body_hash(a) == body_hash(b)


def test_a_redacted_field_cannot_manufacture_a_contract_change():
    """The end-to-end version: same response, different secret, one lookup key."""
    before = {"user": {"email": "alice@example.com"}, "rate": 1.5}
    after = {"user": {"email": "bob@example.org"}, "rate": 1.5}
    ra, _ = redact(before)
    rb, _ = redact(after)
    assert body_hash(ra) == body_hash(rb)
    assert call_key("rates", ra) == call_key("rates", rb)


def test_redaction_actually_fired_somewhere_in_the_corpus():
    """A redaction pass that never removes anything is untested in production.

    GitHub's commit payloads carry author email addresses, so this corpus
    exercises the scanner for real rather than only in a unit test.
    """
    fired = {f"{t}@{v}": read_manifest(t, v)["redaction"]["findings"]
             for t, v in CORPUS}
    assert any(f for f in fired.values()), f"redaction found nothing anywhere: {fired}"
    assert any("email" in f for f in fired.values()), (
        "no email was redacted; the GitHub capture should contain commit author addresses")


def test_no_fixture_contains_an_unredacted_secret_pattern():
    """The scan is only evidence if re-running it over the committed bytes still comes back clean."""
    offenders = []
    for t, v in CORPUS:
        for layer in ("traffic.jsonl",):
            raw = (fixture_dir(t, v) / layer).read_text()
            _clean, found = redact(json.loads(json.dumps(raw)))
            if found:
                offenders.append(f"{t}@{v}/{layer}: {found}")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# Negative control 4: key order is not a contract change
# ---------------------------------------------------------------------------


def test_reordering_json_keys_changes_neither_hash_nor_key():
    """`{"a":1,"b":2}` and `{"b":2,"a":1}` are the same response.

    Canonicalisation makes that true by construction rather than by a later
    comparison step remembering to sort. If it were left to the comparison,
    every consumer would have to remember, and the first one that forgot would
    report a contract change every time a server's serializer reordered a map.
    """
    a = {"base": "USD", "rates": {"EUR": 0.87, "GBP": 0.75}, "date": "2026-09-18"}
    b = {"date": "2026-09-18", "rates": {"GBP": 0.75, "EUR": 0.87}, "base": "USD"}
    assert canonical(a) == canonical(b)
    assert body_hash(a) == body_hash(b)
    assert call_key("rates", a) == call_key("rates", b)


def test_reordering_does_not_flip_any_verdict_in_the_corpus():
    """The end-to-end version: shuffle every recorded object and re-run every check."""
    import random

    def shuffle(o):
        if isinstance(o, dict):
            items = list(o.items())
            random.Random(7).shuffle(items)
            return {k: shuffle(v) for k, v in items}
        if isinstance(o, list):
            return [shuffle(x) for x in o]
        return o

    for t, v in CORPUS:
        rows = load_traffic(t, v)
        shuffled = [shuffle(r) for r in rows]
        for r in _rules(t, v):
            before = evaluate(r["id"], rows)
            after = evaluate(r["id"], shuffled)
            assert before.outcome == after.outcome, (
                f"{t}@{v}:{r['id']} flipped {before.outcome} -> {after.outcome} "
                f"on key reordering alone")


def test_a_real_value_change_does_change_the_hash():
    """The control on the two controls above: canonicalisation must not be so aggressive that it hides an actual difference."""
    a = {"rates": {"EUR": 0.87}}
    b = {"rates": {"EUR": 0.88}}
    assert body_hash(a) != body_hash(b)
    assert call_key("rates", a) != call_key("rates", b)


# ---------------------------------------------------------------------------
# Every rule agrees with its own answer key
# ---------------------------------------------------------------------------


ALL = list(_all_rules())


@pytest.mark.parametrize("tool,version,rule", ALL,
                         ids=[f"{t}:{r['id']}" for t, _v, r in ALL])
def test_each_check_returns_what_the_answer_key_says(tool, version, rule):
    """The answer key states what the tool does; the check reads the recording. They have to agree, or one of them is wrong."""
    want = {"no-mismatch-control": SUPPORTS,
            "unobservable": UNOBSERVABLE}.get(rule.get("mismatch_class"), CONTRADICTS)
    got = evaluate(rule["id"], load_traffic(tool, version))
    assert got.outcome == want, f"{rule['id']}: wanted {want}, got {got.outcome} -- {got.detail}"


@pytest.mark.parametrize("tool,version,rule", ALL,
                         ids=[f"{t}:{r['id']}" for t, _v, r in ALL])
def test_each_verdict_cites_the_exchanges_it_used(tool, version, rule):
    """A verdict with no evidence set cannot be audited, and cannot have its attribution narrowed later."""
    got = evaluate(rule["id"], load_traffic(tool, version))
    keys = {r["key"] for r in load_traffic(tool, version)}
    assert got.evidence, f"{rule['id']} cited no exchanges"
    assert set(got.evidence) <= keys, f"{rule['id']} cited keys not in the recording"


# ---------------------------------------------------------------------------
# Lookup keys: the path is part of the question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,version", CORPUS, ids=[f"{t}@{v}" for t, v in CORPUS])
def test_no_two_exchanges_share_a_lookup_key(tool, version):
    """A collision makes one recorded exchange unreachable, silently.

    Frankfurter v1 hit this: `/v1/2026-09-13` and `/v1/2026-09-11` carry
    identical query parameters and differ only in the date in the path. With
    the path left out of the key they hashed the same, the second overwrote
    the first, and the recording held four exchanges where the capture had
    made five -- including the one demonstrating the defect the fixture exists
    to record. Nothing failed: hashes matched, the replay counted the right
    number of calls because its key list contained the duplicate twice, and a
    probe asking about the Sunday was handed the Friday's answer.
    """
    rows = load_traffic(tool, version)
    keys = [r["key"] for r in rows]
    assert len(set(keys)) == len(keys), (
        f"{len(keys) - len(set(keys))} exchange(s) unreachable on replay")


def test_the_path_is_part_of_the_lookup_key():
    """Same operation, same arguments, different path: two different questions."""
    a = call_key("rates", {"base": "USD"}, "/v1/2026-09-13")
    b = call_key("rates", {"base": "USD"}, "/v1/2026-09-11")
    assert a != b


def test_an_ambiguous_request_is_refused_rather_than_guessed():
    """If one operation and argument set were recorded against several paths, answering with either would answer a question that was not asked."""
    import tempfile

    from agent.adapters.fixture import FixtureAdapter, FixtureMiss

    with tempfile.TemporaryDirectory() as td:
        a = FixtureAdapter(run_id="amb", tool="frankfurter", version="v1-2026-09-19",
                           runs_dir=td)
        with pytest.raises(FixtureMiss, match="was recorded against"):
            a.call("rates", base="USD", symbols="EUR")
        # naming the path resolves it
        status, body = a.call("rates", _path="/v1/2026-09-13", base="USD", symbols="EUR")
        assert status == 200 and body["date"] == "2026-09-11"


def test_a_fixture_with_colliding_keys_is_rejected_at_load():
    """The adapter refuses to build an index that would drop an exchange."""
    import tempfile
    from pathlib import Path as P

    from agent.adapters.fixture import FixtureAdapter

    src = fixture_dir("pokeapi", "v2-2026-09-19")
    with tempfile.TemporaryDirectory() as td:
        dst = P(td) / "pokeapi" / "v2-2026-09-19"
        dst.mkdir(parents=True)
        for f in ("docs.md", "expected.yaml", "manifest.json"):
            (dst / f).write_bytes((src / f).read_bytes())
        rows = [json.loads(l) for l in (src / "traffic.jsonl").read_text().splitlines() if l.strip()]
        rows[1]["key"] = rows[0]["key"]                      # force a collision
        (dst / "traffic.jsonl").write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
        with pytest.raises(ValueError, match="share the key"):
            FixtureAdapter(run_id="x", tool="pokeapi", version="v2-2026-09-19",
                           runs_dir=td, fixtures_root=td)


def test_a_match_on_a_publication_day_does_not_support_the_date_claim():
    """Evidence that could not have shown the defect is not evidence the defect is absent.

    Frankfurter v1 substitutes a different date when the one asked for has no
    published rate. Observing only 2026-09-11 -- a Friday, which has a rate --
    come back as 2026-09-11 says nothing: a publication day could not have
    been substituted. Concluding `supports_doc` from it is the same vacuous
    pass as asserting a page-size cap against a store too small to reach it,
    which is the first bug this project ever found in itself.

    The policy comparison is what surfaced this. Whole-fixture evaluation
    always saw the Sunday too and hid the flaw; buying exchanges one at a time
    produced 52 false beliefs across the arms, all of this shape.
    """
    rows = load_traffic("frankfurter", "v1-2026-09-19")
    friday_only = [r for r in rows if r["path"].endswith("2026-09-11")]
    assert friday_only, "the fixture should contain the publication-day request"

    v = evaluate("non_publication_date_silently_substituted", friday_only)
    assert v.outcome == UNOBSERVABLE, (
        f"a match on a publication day returned {v.outcome}; it can only abstain")
    assert "could not have" in v.detail or "never capable" in v.detail

    sunday = [r for r in rows if r["path"].endswith("2026-09-13")]
    both = friday_only + sunday
    assert evaluate("non_publication_date_silently_substituted", both).outcome == CONTRADICTS


def test_a_post_epoch_date_does_not_support_the_pre_epoch_claim():
    """The same shape, in the lab fixture: the claim is about dates before 1970, so a 1990 date stored correctly tests nothing."""
    rows = load_traffic("lab", "seed1337-2026-09-19")
    faked = []
    for r in rows:
        if r["op"] == "create" and r["request"].get("due_date"):
            r = json.loads(json.dumps(r))
            r["request"]["due_date"] = "1990-01-01"
            r["response"]["due_date"] = "1990-01-01"
        faked.append(r)
    v = evaluate("pre_epoch_date_null", faked)
    assert v.outcome == UNOBSERVABLE, f"got {v.outcome}: {v.detail}"
    assert "pre-epoch" in v.detail
