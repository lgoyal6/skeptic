"""
Change-point detection over observation windows, and drift attribution.

The naive version of this is a diff: compare the last two windows, report what
moved. It is wrong in a specific and expensive way. Tools fail transiently --
a 502, a 429, a timeout -- and a diff cannot tell "the contract changed" from
"one call went wrong", so it promotes noise into permanent belief. The lab's
own 5% 502-after-commit rule is enough to do it, and this project has already
found that exact confusion inside its own probe templates, where an error body
was read as evidence about field storage.

So detection here is explicit about three things:

**Where the change is.** A change point is the index at which a signal takes a
new value *and keeps it*. `find_change_points` scans for a run of consecutive
agreeing windows rather than reacting to the first difference. A value that
flickers back is not a change point; it is noise, and it is reported as
transient.

**What kind of change it is.** Documentation, schema, semantic, and transient
are separated because they cost their reader different things. A docs edit
with unchanged behaviour is a changelog entry. A schema change breaks parsers.
A semantic change leaves the parser working and the logic wrong, which is the
one that gets shipped to production. A transient failure is not a contract
change at all, and is never allowed to open a contract version.

**What evidence caused it.** Attribution narrows to the smallest set of
exchange keys whose signal actually moved -- not the whole window -- so a
reader can go to the specific recorded calls and check for themselves.

## On ordering and clock skew

Windows are compared in the order the timeline supplies, and `observed_at` is
used only for reporting. Wall-clock timestamps come from whichever machine
made the capture, and trusting them would let a clock a few seconds off
reorder a timeline and move an inferred change point. Order is an explicit
property of the timeline, not something recovered from a field that anyone
could have written.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import yaml

from contracts.model import (
    CONFIRMATIONS_REQUIRED,
    Change,
    Contract,
    ContractStore,
    Window,
    schema_of,
)
from fixtures.checks import evaluate, load_traffic
from fixtures.format import content_hash, fixture_dir

DOCUMENTATION = "documentation"
SCHEMA = "schema"
SEMANTIC = "semantic"
TRANSIENT = "transient"

# A status that means "the call did not work right now", as opposed to "this
# is what the API does". These never establish a contract.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Kinds where a single observation is strong enough to promote without waiting
# for repetition. A documentation hash is a fact about a file, not a sample: it
# does not flicker, and requiring two windows to believe it would only delay
# the report. Everything else needs confirmation.
STRONG_KINDS = frozenset({DOCUMENTATION})


# ---------------------------------------------------------------------------
# building windows
# ---------------------------------------------------------------------------


def window_from_fixture(
    tool: str,
    version: str,
    label: str | None = None,
    root: str = "fixtures",
    perturb: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    docs_override: str | None = None,
    rules: list[str] | None = None,
) -> Window:
    """Observe one fixture as a single window.

    `rules` is the rule set to evaluate, which for a timeline is the UNION
    across every version in it rather than each version's own answer key.
    That is what carrying a belief across a version change means: the question
    a v1 belief asks has to keep being asked of v2, or the belief is never
    demoted, merely forgotten. A rule whose evidence the newer capture does
    not contain returns `unobservable`, which is the honest answer -- not
    silently dropping it, and not counting its absence as a refutation.

    `perturb` and `docs_override` exist for the planted-change tests: a real
    timeline needs a real second capture, but proving the classifier separates
    schema drift from semantic drift needs a controlled example of each, and
    waiting for a third party to break their API in the right way is not a
    test strategy.
    """
    rows = load_traffic(tool, version, root)
    if perturb is not None:
        rows = perturb([dict(r) for r in rows])

    docs = (fixture_dir(tool, version, root) / "docs.md").read_text()
    docs_hash = content_hash(docs_override if docs_override is not None else docs)

    schema: dict[str, Any] = {}
    counts: dict[str, int] = {}
    transport: dict[str, list[int]] = {}
    evidence: dict[str, list[str]] = {}
    for r in rows:
        op = r["op"]
        transport.setdefault(op, []).append(r["status"])
        # Only successful responses contribute to the schema. An error body has
        # a shape of its own, and folding it in would make every transient 502
        # look like the response schema changed -- the precise confusion this
        # module exists to prevent.
        if r["status"] < 400:
            s = schema_of(r["response"])
            prev = schema.get(op)
            schema[op] = _merge_schema(prev, s) if prev is not None else s
            counts[op] = counts.get(op, 0) + 1
            evidence.setdefault(f"schema:{op}", []).append(r["key"])

    expected = yaml.safe_load(
        (fixture_dir(tool, version, root) / "expected.yaml").read_text()) or {}
    rule_ids = rules if rules is not None else [r["id"] for r in (expected.get("rules") or [])]
    semantics: dict[str, str] = {}
    for rid in rule_ids:
        v = evaluate(rid, rows)
        semantics[rid] = v.outcome
        evidence[f"semantic:{rid}"] = list(v.evidence)

    evidence["docs"] = []
    return Window(
        tool=tool,
        label=label or version,
        observed_at=0.0,
        docs_sha256=docs_hash,
        schema=schema,
        semantics=semantics,
        transport=transport,
        observed_counts=counts,
        evidence=evidence,
    )


def union_rules(tool: str, versions: list[str], root: str = "fixtures") -> list[str]:
    """Every rule id any of these versions has an answer key for.

    Evaluated at every window so a belief minted under one version keeps being
    asked of the next. Without this, a rule simply vanishes when the newer
    answer key stops listing it, and a stale belief would look retired when it
    was only unexamined.
    """
    out: list[str] = []
    for v in versions:
        exp = yaml.safe_load((fixture_dir(tool, v, root) / "expected.yaml").read_text()) or {}
        for r in exp.get("rules") or []:
            if r["id"] not in out:
                out.append(r["id"])
    return out


def _merge_schema(a: Any, b: Any) -> Any:
    """Union two observations of one operation's shape.

    Two calls to the same operation can legitimately differ -- an optional
    field present in one response and absent in another. Taking only the last
    one would make the schema depend on capture order.
    """
    if a == b:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: _merge_schema(a.get(k), b.get(k)) if k in a and k in b else (a.get(k) or b.get(k))
                for k in sorted(set(a) | set(b))}
    if isinstance(a, list) and isinstance(b, list):
        merged = list(a)
        for x in b:
            if x not in merged:
                merged.append(x)
        return sorted(merged, key=json.dumps)
    return sorted({json.dumps(a), json.dumps(b)})


# ---------------------------------------------------------------------------
# change-point detection
# ---------------------------------------------------------------------------


def find_change_points(windows: list[Window],
                       confirmations: int = CONFIRMATIONS_REQUIRED) -> dict[str, list[int]]:
    """For each signal, the indices where it takes a new value and keeps it.

    "Keeps it" is the whole method. Scanning for the first index where a value
    differs would fire on a single anomalous window and never un-fire. Here a
    candidate at index i is only a change point if the new value holds for
    `confirmations` consecutive windows starting at i -- or if the tail is
    shorter than that and the new value holds to the end, since a change in the
    final window cannot yet be distinguished from noise and is reported as
    unconfirmed rather than silently dropped.
    """
    if len(windows) < 2:
        return {}
    keys: list[str] = []
    for w in windows:
        for k in w.signals():
            if k not in keys:
                keys.append(k)

    points: dict[str, list[int]] = {}
    for key in keys:
        series = [json.dumps(w.signals().get(key), sort_keys=True) for w in windows]
        found: list[int] = []
        current = series[0]
        i = 1
        while i < len(series):
            if series[i] != current:
                run = 1
                j = i + 1
                while j < len(series) and series[j] == series[i]:
                    run += 1
                    j += 1
                if run >= confirmations or j == len(series):
                    found.append(i)
                    current = series[i]
                    i = j
                    continue
                # flickered back: not a change point, and the value that
                # persists is still the old one
                i = j
                continue
            i += 1
        if found:
            points[key] = found
    return points


def transient_signals(windows: list[Window]) -> list[Change]:
    """Statuses that appear in some windows and not others, and never stick.

    Reported so a reader can see the run was noisy, and deliberately never
    returned as a contract change.
    """
    out: list[Change] = []
    ops = sorted({op for w in windows for op in w.transport})
    for op in ops:
        seen = [set(w.transport.get(op, [])) for w in windows]
        transient = set().union(*seen) & TRANSIENT_STATUSES if seen else set()
        for status in sorted(transient):
            present = [i for i, s in enumerate(seen) if status in s]
            if len(present) == len(windows):
                # present everywhere: this is what the API does, not an event.
                continue
            out.append(Change(
                kind=TRANSIENT, signal=f"transport:{op}",
                before="absent", after=status,
                detail=(f"HTTP {status} appeared in {len(present)}/{len(windows)} windows for "
                        f"{op!r} and did not persist; a failure status is an event, not a promise, "
                        f"so it cannot open a contract version"),
                evidence=[k for w in windows for k in w.evidence.get(f"schema:{op}", [])][:2],
            ))
    return out


def classify(before: Window, after: Window) -> list[Change]:
    """What changed between two windows, and of what kind."""
    changes: list[Change] = []

    if before.docs_sha256 != after.docs_sha256:
        behaviour_moved = (before.schema != after.schema or before.semantics != after.semantics)
        changes.append(Change(
            kind=DOCUMENTATION, signal="docs",
            before=before.docs_sha256[:19], after=after.docs_sha256[:19],
            detail=("the documentation hash changed"
                    + ("" if behaviour_moved else
                       " while every observed schema and semantic verdict stayed identical, "
                       "so this is documentation-only drift: a changelog entry, not a break")),
            evidence=[],
        ))

    for op in sorted(set(before.schema) | set(after.schema)):
        b, a = before.schema.get(op), after.schema.get(op)
        if b == a:
            continue
        # An observed schema is a union over the successful calls in the
        # window, which makes it a LOWER BOUND on the real shape. If the later
        # window saw fewer successful calls and the only difference is that
        # fields went missing, the evidence is consistent with those fields
        # simply not appearing in a smaller sample -- absence of evidence, not
        # evidence of absence. Reporting it as a removal is how a transient
        # failure gets promoted into "the API deleted a field", which is the
        # exact bug class this project keeps finding in itself.
        nb, na = before.observed_counts.get(op, 0), after.observed_counts.get(op, 0)
        only_removals = _only_removals(b, a)
        if only_removals and na < nb:
            changes.append(Change(
                kind=TRANSIENT, signal=f"sample:{op}",
                before=f"{nb} successful call(s)", after=f"{na} successful call(s)",
                detail=(f"{op!r} was observed {na} time(s) instead of {nb}, and the only "
                        f"schema difference is fields going missing. A union schema is a "
                        f"lower bound, so a narrower one from a smaller sample is not "
                        f"evidence a field was removed"),
                evidence=after.evidence.get(f"schema:{op}", [])[:2],
            ))
            continue
        changes.append(Change(
            kind=SCHEMA, signal=f"schema:{op}",
            before=b, after=a,
            detail=f"the response shape of {op!r} changed: {_shape_delta(b, a)}",
            evidence=after.evidence.get(f"schema:{op}", [])[:2],
        ))

    for rule in sorted(set(before.semantics) | set(after.semantics)):
        b, a = before.semantics.get(rule), after.semantics.get(rule)
        if b != a:
            changes.append(Change(
                kind=SEMANTIC, signal=f"semantic:{rule}",
                before=b, after=a,
                detail=(f"the verdict on {rule!r} moved from {b} to {a}: the response is still "
                        f"parseable and now means something different"),
                evidence=after.evidence.get(f"semantic:{rule}", []),
            ))

    return changes


def _only_removals(b: Any, a: Any) -> bool:
    """True when every difference is a key present in `b` and missing from `a`."""
    if b == a:
        return False
    if isinstance(b, dict) and isinstance(a, dict):
        if set(a) - set(b):
            return False
        return all(_only_removals(b[k], a[k]) or b[k] == a[k] for k in a)
    return False


def _shape_delta(b: Any, a: Any) -> str:
    if b is None:
        return "the operation was not observed before"
    if a is None:
        return "the operation is no longer observed"
    if isinstance(b, dict) and isinstance(a, dict):
        added, removed = sorted(set(a) - set(b)), sorted(set(b) - set(a))
        retyped = [k for k in set(a) & set(b) if a[k] != b[k]]
        bits = []
        if added:
            bits.append(f"added {added}")
        if removed:
            bits.append(f"removed {removed}")
        if retyped:
            bits.append(f"retyped {sorted(retyped)}")
        return "; ".join(bits) or "reordered only"
    return f"{json.dumps(b)[:60]} -> {json.dumps(a)[:60]}"


# ---------------------------------------------------------------------------
# the timeline
# ---------------------------------------------------------------------------


def build_timeline(windows: list[Window],
                   confirmations: int = CONFIRMATIONS_REQUIRED) -> tuple[ContractStore, list[Change]]:
    """Walk a timeline of windows and produce contract versions plus attribution.

    Deterministic: the same windows in the same order always produce the same
    versions, the same change points, and the same evidence sets.
    """
    if not windows:
        raise ValueError("a timeline needs at least one window")
    store = ContractStore(windows[0].tool)
    store.open(windows[0])

    points = find_change_points(windows, confirmations)
    promote_at: dict[int, list[str]] = {}
    for signal, idxs in points.items():
        for i in idxs:
            promote_at.setdefault(i, []).append(signal)

    all_changes: list[Change] = []
    for i in range(1, len(windows)):
        prev, cur = windows[i - 1], windows[i]
        candidates = classify(prev, cur)
        moving = set(promote_at.get(i, []))

        promoted: list[Change] = []
        for c in candidates:
            persists = c.signal in moving
            strong = c.kind in STRONG_KINDS
            c.promoted = bool(persists or strong)
            c.confirmations = _run_length(windows, c.signal, i)
            if c.promoted:
                promoted.append(c)
            else:
                c.detail += (f" -- observed once and not confirmed over "
                             f"{confirmations} windows, so it is a candidate, not a contract")
            all_changes.append(c)

        if promoted:
            store.open(cur, promoted)
        else:
            store.extend(cur)

    all_changes.extend(transient_signals(windows))
    return store, all_changes


def _run_length(windows: list[Window], signal: str, start: int) -> int:
    series = [json.dumps(w.signals().get(signal), sort_keys=True) for w in windows]
    n = 1
    for j in range(start + 1, len(series)):
        if series[j] == series[start]:
            n += 1
        else:
            break
    return n
