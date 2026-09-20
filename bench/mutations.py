"""
Mutation testing: restore each old bug and prove the suite notices.

A passing test suite says nothing about whether the tests can fail. This
project has already shipped one test that could not: an `xfail(strict=False)`
marker meant the suite was green whether the defect it documented was present
or absent, for as long as anyone cared to look. The fix was not to delete the
marker and trust the result -- it was to remove the guard, watch the test fail,
put the guard back, and watch it pass.

This module makes that check a command instead of a ritual. Each `Mutation`
names a fix, the exact source edit that restores the behaviour before the fix,
and the tests that must fail when it does. Running it copies the tree to a
temporary directory, applies one mutation, runs only the named tests, and
reports whether they died. A mutation nothing notices is a fix with no
regression protection, which is the thing being looked for.

    ./.venv/bin/python -m bench.mutations
    ./.venv/bin/python -m bench.mutations --only shared_rate_budget

The edits are literal string replacements against the current source. If a
replacement no longer matches, that is reported as `NOT APPLIED` rather than
silently skipped: a mutation that cannot be applied proves nothing, and a
green run full of unapplied mutations is exactly the false comfort this file
exists to prevent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Mutation:
    name: str
    fix: str                      # the commit/behaviour this protects
    mismatch_class: str           # which defect family, for coverage reporting
    path: str
    old: str                      # text present in the fixed source
    new: str                      # text that restores the pre-fix behaviour
    must_fail: list[str] = field(default_factory=list)


MUTATIONS: list[Mutation] = [
    Mutation(
        name="apply_uninformative_guard",
        fix="the uninformative-observation guard lives in _apply, the point of effect",
        mismatch_class="vacuous-measurement",
        path="reflect/probe.py",
        old="    if _facts_uninformative(obs_facts):",
        new="    if False and _facts_uninformative(obs_facts):",
        must_fail=["test_apply_itself_guards_against_uninformative_observation"],
    ),
    Mutation(
        name="timing_status_gate",
        fix="a read that failed is not a read that changed",
        mismatch_class="status-blindness",
        path="reflect/templates.py",
        old="    if n0 is None or n1 is None:",
        new="    if False and (n0 is None or n1 is None):",
        must_fail=["test_timing_rate_limited_read_is_not_eventual_consistency"],
    ),
    Mutation(
        name="boundary_empty_sweep",
        fix="a create sweep where nothing committed observed nothing",
        mismatch_class="vacuous-measurement",
        path="reflect/templates.py",
        old="        if not usable:",
        new="        if False and not usable:",
        must_fail=["test_boundary_create_sweep_with_no_committed_row_observed_nothing"],
    ),
    Mutation(
        name="consistency_status_gate",
        fix="a 502-after-commit cannot confirm silent coercion",
        mismatch_class="status-blindness",
        path="reflect/templates.py",
        old='    if sc != 200 or not isinstance(created, dict) or "id" not in created:',
        new='    if False and (sc != 200 or not isinstance(created, dict) or "id" not in created):',
        must_fail=["test_consistency_502_after_commit_cannot_confirm_silent_coercion"],
    ),
    Mutation(
        name="shared_rate_budget",
        fix="one process-wide request budget, not a per-adapter pacer",
        mismatch_class="rate-limit",
        path="agent/adapters/budget.py",
        old="                if len(self._sent) < self.n:",
        new="                if True:",
        must_fail=["test_four_workers_stay_within_one_global_budget"],
    ),
    Mutation(
        name="burst_takes_budget_exclusively",
        fix="a bursting probe holds the budget instead of racing siblings",
        mismatch_class="rate-limit",
        path="agent/adapters/budget.py",
        old="        self._lock.acquire()\n        try:\n            yield",
        new="        try:\n            yield",
        must_fail=["test_bursting_probe_takes_the_budget_instead_of_racing_siblings"],
    ),
    Mutation(
        name="unknown_field_guard_literal",
        fix="compile the unknown-field guard from evidence, not a field name",
        mismatch_class="unknown-field",
        path="shim/guards.py",
        old='    ("silent_coercion", UNKNOWN_UPDATE_FIELD, RejectUnknownUpdateField,',
        new='    ("silent_coercion", "not_a_real_field", RejectUnknownUpdateField,',
        must_fail=[
            "test_unknown_field_guard_compiles_for_any_field_name",
            "test_unknown_field_guard_is_matched_on_evidence_not_a_field_blacklist",
        ],
    ),
    Mutation(
        name="retire_cap_polarity",
        fix="the retirement demo answers the question its caller asks",
        mismatch_class="pagination",
        path="bench/retire_demo.py",
        old="    return not still_capped, (",
        new="    return still_capped, (",
        must_fail=["test_retire_demo_cap_check_answers_the_question_its_caller_asks"],
    ),
    Mutation(
        name="retire_population_refusal",
        fix="a population too small to exercise the rule gets no verdict",
        mismatch_class="vacuous-measurement",
        path="bench/retire_demo.py",
        old="    if population <= PAGE_SIZE_CAP:",
        new="    if False and population <= PAGE_SIZE_CAP:",
        must_fail=["test_retire_demo_cap_check_refuses_an_insufficient_population"],
    ),
    Mutation(
        name="idempotent_create_double_search",
        fix="the guard does not pay for a discarded search",
        mismatch_class="wasted-call",
        path="shim/guards.py",
        old="        try:\n            r = adapter.client.post(",
        new=(
            "        try:\n"
            "            sc, found = adapter.client.post(\n"
            '                "/v1/search",\n'
            '                json={"filter": {"vendor": vendor} if vendor else {}, "page_size": 50,\n'
            '                      "include_archived": True},\n'
            "            ).status_code, None\n"
            "        except Exception:\n"
            "            return body\n"
            "        try:\n"
            "            r = adapter.client.post("
        ),
        must_fail=["test_idempotent_create_guard_does_not_pay_for_a_discarded_search"],
    ),
    Mutation(
        name="redaction_is_a_constant",
        fix="a redaction carries no information about what it replaced",
        mismatch_class="corpus-integrity",
        path="fixtures/format.py",
        old="                    s = pat.sub(REDACTED, s)",
        new='                    s = pat.sub(lambda m: f"<redacted:{len(m.group(0))}>", s)',
        must_fail=["test_redaction_writes_a_constant_so_it_carries_no_information",
                   "test_a_redacted_field_cannot_manufacture_a_contract_change"],
    ),
    Mutation(
        name="canonicalisation_sorts_keys",
        fix="reordering JSON keys is not a contract change",
        mismatch_class="corpus-integrity",
        path="fixtures/format.py",
        old='    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)',
        new='    return json.dumps(obj, sort_keys=False, separators=(",", ":"), default=str)',
        must_fail=["test_reordering_json_keys_changes_neither_hash_nor_key"],
    ),
    Mutation(
        name="transient_needs_confirmation",
        fix="a change point is where a value takes hold, not where it first differs",
        mismatch_class="drift-detection",
        path="contracts/drift.py",
        old="                if run >= confirmations or j == len(series):",
        new="                if True:",
        must_fail=["test_transient_failure_is_never_a_contract_change",
                   "test_a_flicker_is_not_a_change_point"],
    ),
    Mutation(
        name="error_bodies_excluded_from_schema",
        fix="an error body has its own shape and is not part of the response schema",
        mismatch_class="drift-detection",
        path="contracts/drift.py",
        old='        if r["status"] < 400:',
        new="        if True:",
        must_fail=["test_an_error_body_is_not_part_of_the_response_schema",
                   "test_a_window_of_only_failures_reports_no_schema_rather_than_an_error_schema"],
    ),
    Mutation(
        name="narrowing_under_a_smaller_sample",
        fix="a union schema is a lower bound; a narrower one from fewer calls is not a removal",
        mismatch_class="drift-detection",
        path="contracts/drift.py",
        old="        if only_removals and na < nb:",
        new="        if False and only_removals and na < nb:",
        must_fail=["test_a_narrower_schema_from_a_smaller_sample_is_not_a_removed_field"],
    ),
    Mutation(
        name="ordering_ignores_observed_at",
        fix="clock skew cannot move an inferred change point",
        mismatch_class="drift-detection",
        path="contracts/drift.py",
        old="    if len(windows) < 2:\n        return {}",
        new=("    if len(windows) < 2:\n        return {}\n"
             "    windows = sorted(windows, key=lambda w: w.observed_at)"),
        must_fail=["test_clock_skew_cannot_move_the_change_point"],
    ),
    Mutation(
        name="budget_worst_window_can_report_a_violation",
        fix="the budget's grant log can actually show an over-limit window",
        mismatch_class="rate-limit",
        path="agent/adapters/budget.py",
        old="        return max(sum(1 for t in g if start <= t < start + self.window_s) for start in g)",
        new="        return 0",
        must_fail=["test_a_broken_budget_is_caught_by_the_same_assertion"],
    ),
    Mutation(
        name="path_is_part_of_the_lookup_key",
        fix="two exchanges differing only in path are two different questions",
        mismatch_class="corpus-integrity",
        path="fixtures/format.py",
        old='        f"{op}\\n{path}\\n{canonical(request or {})}".encode()).hexdigest()[:24]',
        new='        f"{op}\\n{canonical(request or {})}".encode()).hexdigest()[:24]',
        must_fail=["test_the_path_is_part_of_the_lookup_key"],
    ),
    Mutation(
        name="date_check_requires_a_date_at_risk",
        fix="a match on a publication day cannot support a claim about substitution",
        mismatch_class="vacuous-measurement",
        path="fixtures/checks.py",
        old="    if not at_risk:",
        new="    if False and not at_risk:",
        must_fail=["test_a_match_on_a_publication_day_does_not_support_the_date_claim"],
    ),
    Mutation(
        name="eig_does_not_beat_greedy",
        fix="the published negative result is asserted, not described",
        mismatch_class="evaluation",
        path="bench/policies.py",
        old='    "greedy": greedy,',
        new='    "greedy": expected_information_gain,',
        must_fail=["test_greedy_beats_both_naive_baselines"],
    ),
    Mutation(
        name="pagination_guard_knows_the_end_of_data",
        fix="a short page at the end of the data is not a silent clamp",
        mismatch_class="guard-precision",
        path="shim/corpus_guards.py",
        old="    if _more_exists(ex, n):",
        new="    if True:",
        must_fail=["test_the_pagination_guard_tells_a_clamp_from_the_end_of_the_data"],
    ),
    Mutation(
        name="guard_respects_disclosure",
        fix="a clamp that announces itself is not a mismatch",
        mismatch_class="guard-precision",
        path="shim/corpus_guards.py",
        old="    if announced:\n        return None",
        new="    if False:\n        return None",
        must_fail=["test_the_pagination_guard_tells_a_clamp_from_the_end_of_the_data"],
    ),
    Mutation(
        name="guards_only_from_confirmed_mismatches",
        fix="an abstention does not compile to enforcement",
        mismatch_class="abstention",
        path="shim/corpus_guards.py",
        old="        if verdict.outcome != CONTRADICTS:\n            continue",
        new="        if False:\n            continue",
        must_fail=["test_a_supported_claim_compiles_no_guard_even_when_a_body_exists",
                   "test_an_abstention_compiles_no_guard"],
    ),
    Mutation(
        name="replay_does_not_persist_when_reading",
        fix="reporting has no write side effect",
        mismatch_class="side-effect",
        path="replay/counterfactual.py",
        old="    if save:\n        store.save()",
        new="    if True:\n        store.save()",
        must_fail=["test_the_suite_does_not_write_to_the_repository"],
    ),
    Mutation(
        name="unobservable_verdict_exists",
        fix="an unobservable claim abstains instead of guessing",
        mismatch_class="abstention",
        path="fixtures/checks.py",
        old='UNOBSERVABLE = "unobservable"',
        new='UNOBSERVABLE = "supports_doc"',
        must_fail=["test_abstention_is_reachable_and_not_merely_declared"],
    ),
]


def _copy_tree(dst: Path) -> None:
    ignore = shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "*.pyc", ".pytest_cache",
        "runs", "probes", "node_modules", ".agent-work", "ui",
    )
    shutil.copytree(REPO, dst, ignore=ignore, dirs_exist_ok=True)


def run_one(m: Mutation, python: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"skeptic-mut-{m.name}-") as td:
        tree = Path(td) / "repo"
        _copy_tree(tree)
        target = tree / m.path
        src = target.read_text()
        if m.old not in src:
            return {"name": m.name, "applied": False,
                    "detail": f"anchor not found in {m.path}: {m.old[:60]!r}"}
        target.write_text(src.replace(m.old, m.new, 1))

        sel = " or ".join(m.must_fail)
        proc = subprocess.run(
            [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests", "-k", sel],
            cwd=tree, capture_output=True, text=True, timeout=600,
        )
        out = proc.stdout + proc.stderr
        # pytest prints one FAILED line per failing test; require every named
        # test to be among them, not merely that something failed.
        failed_lines = {l.split("::")[-1].split()[0]
                        for l in out.splitlines() if l.startswith("FAILED")}
        killed = all(t in failed_lines for t in m.must_fail)
        ran = "no tests ran" not in out
        return {"name": m.name, "applied": True, "killed": killed, "ran": ran,
                "must_fail": m.must_fail, "detail": out.strip().splitlines()[-1] if out.strip() else ""}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="run one mutation by name")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()

    chosen = [m for m in MUTATIONS if a.only in (None, m.name)]
    if not chosen:
        print(f"  no mutation named {a.only!r}")
        return 2

    print(f"\n  mutation testing -- restore each old bug, prove a test dies\n")
    print(f"  {'mutation':<34} {'class':<20} result")
    print(f"  {'-'*34} {'-'*20} {'-'*28}")
    bad: list[str] = []
    by_class: dict[str, list[bool]] = {}
    for m in chosen:
        r = run_one(m, a.python)
        if not r["applied"]:
            verdict, ok = "NOT APPLIED", False
        elif not r["ran"]:
            verdict, ok = "NO TEST RAN", False
        elif r["killed"]:
            verdict, ok = "killed (test failed)", True
        else:
            verdict, ok = "SURVIVED", False
        by_class.setdefault(m.mismatch_class, []).append(ok)
        if not ok:
            bad.append(f"{m.name}: {verdict} -- {r.get('detail','')}")
        print(f"  {m.name:<34} {m.mismatch_class:<20} {verdict}")

    print(f"\n  coverage by mismatch class:")
    for cls, oks in sorted(by_class.items()):
        print(f"    {cls:<22} {sum(oks)}/{len(oks)} mutations killed")

    if bad:
        print(f"\n  {len(bad)} mutation(s) not caught:")
        for b in bad:
            print(f"    - {b}")
        return 1
    print(f"\n  all {len(chosen)} mutations killed: every fix has a test that fails without it\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
