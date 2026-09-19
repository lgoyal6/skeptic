# skeptic: correctness audit

## Summary

The measurement spine that the project's own README leans on hardest — deterministic
anomaly detection in `agent/contract.py`, the `_uninformative`/`_facts_uninformative`
guard, structural (not LLM-judged) scoring in `bench/score.py` — is sound, and the
project's public self-criticism (the five documented instances of "measuring without
establishing observability", the stale-xfail confession in the README) is accurate as
far as it goes. But it does not go far enough: this audit found a **sixth and seventh**
instance of the exact same bug class, both demonstrated live against a running
instance of the lab, neither mentioned anywhere in the repo. The most serious one
(`reflect/templates.py::_consistency` and the create-branch of `_boundary`) sits
inside the experiment layer that is supposed to be the credible part of the
pipeline — the one that turns a hypothesis into a confirmed belief by designed
experiment rather than vibes — and it can manufacture a false "silently coerced to
null" verdict out of nothing more than the lab's own well-known, intentional 5%
502-after-commit rule, no bad luck or adversarial input required (it reproduced on
the 6th of 200 tries, single-threaded, zero concurrency). A second, independent bug
in `bench/retire_demo.py`'s page-size check can make the unlearning demo behave
backwards. A third bug in `shim/guards.py`'s registry means a correctly confirmed,
correctly *scored* belief about `unknown_field_ignored` can silently compile to zero
enforcement in the exported shim, undermining the specific claim ("if the agent
learned it, the shim enforces it") the module's own docstring makes. None of these
three would show up in the numbers currently in `README.md`, because none of them
happened to be exercised in the runs that produced those numbers — which is exactly
how this class of bug hides. Recommendation: do not trust `bench/settle.py` output,
or `bench/retire_demo.py`'s verdict for `page_size_cap`, until these are fixed.

## Findings table

| severity | location | description |
| --- | --- | --- |
| HIGH | `reflect/templates.py:202-236` (`_consistency`) | Doesn't check the create call's HTTP status before treating the response body as the created item; a 502-after-commit or 429 renders every sent field as `stored: null`, indistinguishable from real silent-coercion evidence, and `_facts_uninformative` does not catch it. Reproduced single-threaded on try 6/200. |
| HIGH | `reflect/templates.py:110-119` (`_boundary`, create branch) | Same root cause as above: a non-200 `create` response makes `stored` come back `None`, which reads exactly like the field was silently nulled. Reproduced on try 4/300 with zero concurrency. |
| HIGH | `bench/retire_demo.py:167-173` (`_check_page_size_cap`) | Measures the page-size cap against the whole, unfiltered, un-seeded lab store instead of a guaranteed-populated vendor. When the store holds ≤50 total rows (the normal state right after a lab reset, since this function never seeds data), it reports "cap still present" even when the rule has genuinely been disabled — the opposite of what the retirement demo exists to show. Reproduced live against a fresh lab instance. |
| MEDIUM | `shim/guards.py:337-338` (`REGISTRY`) | The guard that implements `unknown_field_ignored` is gated on the belief's `parameter` being the literal string `"not_a_real_field"` — an artifact of one hardcoded probe in `reflect/recon.py` — rather than any of the fixed API field names every other registry entry uses. A belief confirmed via any other path (an agent task, the LLM-designed `consistency` template, `reflect/reopen.py`) uses whatever field name was actually tried and silently compiles to zero guards even though it is correctly confirmed and correctly scored. Demonstrated directly. |
| MEDIUM | `bench/settle.py` (whole module, concurrency model) | `--workers N` (default 4) runs N `LabAdapter`s concurrently, each pacing itself independently against the tool's 3-req/s limit under the assumption it is the only client. The limit is global to the shared lab process, so concurrent probes trip each other's rate limiter — measured 23/32 (72%) calls returning 429 across 4 adapters each individually paced under the per-adapter budget. Combined with the two `_consistency`/`_boundary` findings above, this means the *documented, default* way of running settle is actively likely to manufacture false coercion/null verdicts, not just a theoretical risk. |
| LOW | `reflect/templates.py:140-162` (`_timing`) | Same status-blindness family: `n0`/`n1` are set to `None` on a non-200 response, and `"changed": n0 != n1` will be `True` whenever exactly one side failed — a rate-limited read looks identical to "eventual consistency observed", though this is not independently confirmed as reachable end-to-end (not gated by `_facts_uninformative` either). |
| LOW | `shim/guards.py:181-197` (`IdempotentCreate.after`) | Makes the identical `/v1/search` call twice; the first result (`sc`) is computed and discarded (`found` is unconditionally `None`). Dead code / wasted HTTP call, not a correctness bug — it happens inside a guard's `.after()`, invisibly to the run's call count. |
| LOW | `reflect/templates.py:45` (`ProbeTemplate.描述`) | Stray non-ASCII dataclass field, explicitly commented "placeholder guard, unused". Confirmed dead: no constructor anywhere sets it, nothing reads it. Harmless but confusing (easy to mistake for the real `description` field two lines below). |
| LOW | `tests/test_invariants.py:140-174` (`test_apply_alone_does_not_guard_against_uninformative_observation`) | Marked `xfail(strict=False)`, which means the test can never fail the suite regardless of whether the bug it documents is present or absent (present → xfail, green; absent → xpass, still green under `strict=False`). Already correctly disclosed in `README.md` §"What was found by running it" item 5 as a "stale comment", but the test itself provides zero regression protection either way, which the README does not say. |

No findings rise above HIGH; none of the three HIGH/MEDIUM findings above were invented to fill space, and several plausible leads (see "Checked and found correct" below) turned out, on verification, not to be bugs.

## HIGH: `_consistency` and `_boundary`'s create branch treat any non-200 create as if it succeeded and returned the field as null

**What the code does.** `reflect/templates.py::_consistency`:

```python
sc, created = a.create(**fields)
item_id = (created or {}).get("id") if isinstance(created, dict) else None
echo = {k: (created or {}).get(k) for k in fields} if isinstance(created, dict) else {}
...
mismatches = {
    k: {"sent": v, "stored": echo.get(k)}
    for k, v in fields.items()
    if echo.get(k) != v
}
```

`sc` (the HTTP status) is captured and never checked again. Every error body the lab
returns is itself a `dict` — `{"error": "bad_gateway", "message": "upstream failure"}`
for a 502, `{"error": "rate_limited", "message": "too many requests"}` for a 429 — so
`isinstance(created, dict)` is `True` even on failure, and `echo.get(k)` for every
field the caller sent comes back `None` because none of those keys exist in the error
body. The result is indistinguishable from "every sent field was silently nulled by
the server."

`_boundary`'s create-style branch has the identical gap:

```python
sc, body = a.create(**args)
got = (body or {}).get(param) if isinstance(body, dict) else None
stored = len(got) if isinstance(got, str) else got
```

**Why it is wrong.** The lab has a rule, `write_502_after_commit`, that is one of
the 14 intentional discrepancies and fires on roughly 5% of *all* creates by design
(`lab/server.py:248`, `WRITE_502_RATE = 0.05`). It is not a rare edge case; any probe
that issues more than a couple of dozen creates will very likely hit it. `_consistency`
is exactly the template the probe designer picks to test `silent_coercion` /
`undocumented_flag` hypotheses (see `TEMPLATES["consistency"].splits` in
`reflect/templates.py:348-354`), i.e. the same template used to confirm beliefs like
`pre_epoch_date_null` and `large_number_to_string`. Neither `_facts_uninformative`
(`reflect/probe.py:145-156`) nor anything else in the pipeline inspects
`create_status`; it only checks `facts.get("error")` (a top-level key the observation
never sets for this failure mode), an all-zero `sweep` list (not present in
`_consistency`'s facts shape), or `population == 0` (also absent). So a record like
this sails straight past the uninformative guard and into `VERDICT_PROMPT`, presented
to the reflector as legitimate wire evidence that a field was silently nulled.

**Concrete scenario.** A designed experiment to confirm/falsify a hypothesis about,
say, `due_date` or `amount` picks the `consistency` template. On the try where the
create happens to draw a 502 (5% chance per create, effectively guaranteed over a
run), the probe reports `mismatches: {due_date: {sent: ..., stored: null}, ...}` and
`echo_matches_sent: False` for every field it sent, not just the one under test. The
reflector, asked to judge this against a rival hypothesis, can plausibly confirm a
*false* `silent_coercion` belief for a field that behaves completely normally, or
confirm the right belief for the wrong reason (mistaking the well-documented
idempotency hazard for a coercion bug on an unrelated field) — precisely the
"plausible-sounding prose with no support in the wire evidence" failure the project
says it built `constrain_class()` to prevent (`reflect/hypothesis.py:149-171`), except
this route bypasses `constrain_class` entirely because the anomaly-kind machinery
never runs; the templates hand facts straight to a free-form verdict prompt.

**Demonstrated.** Single-threaded, no concurrency, against a freshly reset isolated
lab instance:

```
single-threaded, no concurrency at all -- hit a non-200 create after 6 tries
create_status: 502
mismatches: {'title': {'sent': 'solo-victim-5', 'stored': None}, 'amount': {'sent': 999, 'stored': None}, 'vendor': {'sent': 'Solo502', 'stored': None}}
echo_matches_sent: False
```

and for `_boundary`'s create branch:

```
non-200 create hit after 4 tries: {'sent': 500003, 'status': 429, 'stored': None, 'stored_type': 'NoneType'}
```

**Smallest fix.** In both functions, gate on `sc == 200` before computing `echo`/
`mismatches` (or `stored`), and report the non-200 case as its own fact
(`{"create_status": sc, "error": body}`) so `_facts_uninformative` can recognize it —
that function already checks `facts.get("error")`, so simply setting that key on a
failed create closes the gap with a one-line change per function, no new guard logic
needed.

## HIGH: `bench/retire_demo.py`'s page-size check ignores population entirely

**What the code does.**

```python
def _check_page_size_cap(adapter: LabAdapter) -> tuple[bool, str]:
    status, resp = _retry_429(lambda: adapter.search(page_size=100, sort="last_edited"))
    if status != 200:
        return False, f"search failed with status {status}"
    n = len(resp.get("results", []))
    msg = f"requested page_size=100, got {n} results back"
    return n > 50, msg
```

There is no `filter`, no vendor, and no seeding anywhere in the retirement demo's
call path for this rule — it reads whatever total row count the shared lab happens
to hold at the moment the demo runs, and treats "more than 50 rows came back" as
proof the cap is off.

**Why it is wrong.** This is exactly the bug class the project names five times over
in `tests/test_invariants.py`'s docstring: measuring a cap without first establishing
that enough rows exist to exercise it. Every other check in `bench/retire_demo.py`
(`_check_include_archived_flag`, `_check_archived_get_404`, etc.) creates its own
test data per trial and only ever reasons about that data, so they are immune to this.
`_check_page_size_cap` is the one exception, and it happens to be the one rule in
`PRIORITY` that structurally *needs* population to mean anything.

**Concrete scenario.** `run_demo` disables the `page_size_cap` rule
(`_set_rule(lab_base, rule_id, False)`) and then calls `check_behaviour` /
`_check_page_size_cap` in a loop, feeding the result to
`belief.observe_support_for_doc` or `belief.observe_contradiction`
(`bench/retire_demo.py:262-266`). If the lab's total item count is at or below 50 at
that moment — which is the normal state right after any `/_control/reset`, since
nothing in this file's own call path seeds data — the function returns `False`
(`n > 50` is false) *regardless of whether the cap is on or off*, which is read as
`observe_contradiction`, i.e. "the old lie persists." This is backwards: the rule was
just disabled, the docs are now correct, and the demo will never detect it, so the
belief can never cross `DOC_TRUST_THRESHOLD` and retire — defeating the entire
purpose of the demo whenever it is invoked in a low-population lab, which is the
common case since the file provides no seeding of its own.

**Demonstrated**, against a fresh, isolated lab instance seeded with only 12 items
(well below the 50-row cap) with the rule explicitly disabled:

```
cap rule enabled: False
world_matches_docs = False   message: requested page_size=100, got 12 results back
EXPECTED: cap is OFF, so world_matches_docs should be True (docs promise up to 500).
Got False even though the cap is genuinely disabled, purely because the store only has 12 rows total.
```

This did not manifest in the `bench/retire_result.json` currently on disk only
because `PRIORITY` picked `pre_epoch_date_null` first (an earlier-priority rule with
a per-trial, self-seeding check) in that particular run — the bug is latent, not
already triggered, but it will fire the moment `page_size_cap` is the chosen target
in a lab that hasn't independently accumulated >50 rows.

**Smallest fix.** Seed (or verify) at least 51+ rows for a private vendor before the
sweep, and filter the search to that vendor, mirroring what
`reflect/templates.py::_ensure_population` already does correctly for the probe
pipeline — this file could call that same helper instead of reinventing (incorrectly)
a population-free version.

## MEDIUM: `shim/guards.py`'s registry match for `unknown_field_ignored` is pinned to one hardcoded string

**What the code does.**

```python
REGISTRY: list[...] = [
    ...
    ("silent_coercion", "not_a_real_field", RejectUnknownUpdateField,
     {"name": "reject_unknown_update_field"}),
    ...
]

def compile_guards(store: BeliefStore, only_confirmed: bool = True) -> list[Guard]:
    ...
    for b in beliefs:
        for cls, param, klass, kw in REGISTRY:
            if b.cls != cls:
                continue
            if param is not None and b.parameter != param:
                continue
            ...
```

Every other `param` in `REGISTRY` (`page_size`, `filter`, `amount`, `due_date`,
`sort`, `title`, `items`) is a fixed field of the documented API surface — the
anomaly machinery always assigns that exact string regardless of who triggered it.
`unknown_field_ignored` is different: its `parameter` is whatever key the caller
happened to send that the schema doesn't recognise
(`agent/contract.py`'s `_check_cross_call`: `unknown = [k for k in (c.request or {})
if k not in self.spec["promises"]["echo_fields"]]`), which varies by caller.
`reflect/recon.py:111` happens to use the literal field name `not_a_real_field`;
`bench/retire_demo.py:158` uses a *different* literal name,
`not_a_real_field_xyz`; an agent task, the LLM-designed `consistency` template, or
`reflect/reopen.py`'s re-proposal would use whatever the model or the run picked.

**Why it is wrong.** `bench/score.py`'s ground-truth match for this rule uses a
wildcard parameter (`parameter: "*"` in `lab/ground_truth.yaml`), so a belief about
this rule scores correctly (counts toward recall/precision) no matter what field name
was used to discover it. But `compile_guards` requires an *exact* literal match, so
the exported guard silently fails to compile for any belief not minted through the
one specific recon.py code path. This directly contradicts the module's own claim
(`shim/guards.py:8-12`): "if the agent never learned it, the shim does not enforce
it" implies the converse holds too — it does not.

**Demonstrated** — a belief that is correctly confirmed (and would score correctly
against ground truth) fails to compile to any guard because it carries a realistic,
different field name:

```python
b = Belief(..., cls='silent_coercion', operation='update',
           parameter='nonexistent_field',  # <- the field name actually used
           status=Status.CONFIRMED, ...)
guards = compile_guards(store)
# compiled guard names: []
```

**Smallest fix.** Match on `b.cls == "silent_coercion" and b.operation == "update"`
for this one registry entry, without a parameter constraint (mirroring how the ground
truth itself treats this rule's parameter as a wildcard), rather than requiring an
exact string.

## MEDIUM: concurrent `bench/settle.py` workers trip each other's rate limiter

**What the code does.** `bench/settle.py` fans probes out across a
`ThreadPoolExecutor` (`--workers`, default 4), and each probe gets its own
`LabAdapter` with `min_interval=0.4` (or `0.0` for `header_burst`) — pacing computed
against `RATE_LIMIT_N = 3` requests per `RATE_LIMIT_WINDOW_S = 1.0` second
(`lab/server.py:46-47`) as if each adapter were the only caller. The limit is
enforced globally, in `lab/server.py`'s single shared `req_times` deque
(`lab/server.py:181-205`), across every client hitting the process.

**Why it is wrong.** Four adapters each individually paced at 2.5 req/s sum to
10 req/s against a process-wide 3 req/s budget. Every probe template's status
handling downstream (as detailed in the two HIGH findings above, plus `_timing`) can
misread a 429 as meaningful signal rather than as the noise it is, and this failure
mode is invoked precisely when running the documented default (`bench/settle.py`'s
own module docstring example is `--workers 4`).

**Demonstrated**, four adapters each individually paced under the per-adapter budget,
run concurrently against an isolated fresh lab instance:

```
elapsed 2.8s
  worker 2 (min_interval=0.4, believes itself alone): [429, 429, 429, 200, 429, 429, 200, 429]
  worker 1 (min_interval=0.4, believes itself alone): [200, 429, 429, 200, 429, 429, 200, 429]
  worker 0 (min_interval=0.4, believes itself alone): [200, 429, 429, 200, 429, 429, 429, 429]
  worker 3 (min_interval=0.4, believes itself alone): [200, 429, 429, 429, 429, 429, 200, 429]
total 429s across 4 concurrent probes that each individually paced under the 3 req/s cap: 23/32
```

**Smallest fix.** Coordinate pacing across workers with a process-wide token bucket
(e.g. a shared `threading.Semaphore`/timestamp deque passed to every `LabAdapter` in
a `settle` run), or reduce `--workers` to 1 for anything touching `create`, since the
consequence is not merely slower probes but silently corrupted `_consistency`/
`_boundary` observations as shown above.

## What was checked and found correct

- **`agent/contract.py`'s core detection layer does gate on status.** `_check_echo`
  (used during real task/recon runs, as opposed to the standalone probe templates)
  explicitly requires `c.status != 200: return out` before comparing echoed fields —
  the exact check missing from `reflect/templates.py`. The bug found above is scoped
  to the probe/experiment layer, not the original anomaly-detection layer.
- **The cursor-expiry substring bug (#2) is genuinely fixed and tested.**
  `agent/contract.py`'s status check requires the literal `error == "cursor_expired"`,
  not a substring match, and `tests/test_invariants.py::test_cursor_expiry_requires_exact_error_not_a_substring_match`
  exercises both the false-positive (`invalid_cursor`) and true-positive
  (`cursor_expired`) cases.
- **`reflect/templates.py::_ensure_population` and its guard actually work as
  designed**, including in the case I suspected might be a gap: a `boundary` probe
  whose `base` carries no `filter.vendor` (plausible from the LLM's design prompt,
  which does not mandate one). `_ensure_population` returns `0` in that case rather
  than silently seeding nothing and reporting a nonzero population, and
  `_facts_uninformative`'s `population == 0` check catches it downstream, so this
  path is safely neutralised end to end — verified directly against an isolated lab
  with an unfiltered 20-row sweep.
- **`bench/apply_probes.py` no longer bypasses the uninformative guard** (this was
  bug #5 in the project's own history). The guard now lives in `_apply` itself
  (`reflect/probe.py:391-414`), which is exactly what both `bench/settle.py` and
  `bench/apply_probes.py` call, so the "guard only in `run_probe`, bypassed via
  disk-replay" failure mode is closed. `tests/test_invariants.py`'s xfail test
  confirms this empirically (`1 xpassed` on a clean `pytest` run), and the README
  discloses this candidly, including the fact that the xfail's own docstring is now
  stale text — accurate self-reporting, not a hidden problem (see the LOW finding
  above about that test providing no actual regression signal either way).
- **`bench/score.py`'s recall denominator is honest.** `observable_rules()` unions
  the live `/_control/rules_fired` response with a persisted `runs/observable.json`,
  falling back to all 14 rules only when nothing has ever been observed — this
  correctly avoids both a stale "only 1 rule fired" collapse and an unearned "never
  observed, so don't count it against us" free pass.
- **`bench/ab.py` and `bench/ablate.py` reset the world identically for every
  task/arm.** Both call `_reset_world()` (`/_control/reset`, fixed seed 1337) before
  `task.setup(cfg)` runs for every repetition of every task, in both arms, so no state
  leaks between the naive/shielded arms or between memory/wiped/shuffled arms. The
  ablation's shuffled-store construction (`_make_shuffled_store`) correctly builds a
  fixed-point-free derangement and snapshots original text before mutating in place,
  avoiding the "already-overwritten belief becomes a donor" self-undoing bug that
  in-place permutation code commonly has.
- **`reflect/hypothesis.py::constrain_class` and its test coverage are sound.** Every
  anomaly kind `agent/contract.py` can actually emit has a `KIND_ALLOWS` entry
  (enforced by `tests/test_invariants.py::test_every_contract_anomaly_kind_has_a_class_constraint`,
  which regex-scans `contract.py` itself rather than trusting a hardcoded list), so
  the reflector cannot mint a belief in a class its own wire evidence does not
  support.
- **`replay/counterfactual.py` is deliberately conservative** and does not overclaim:
  its `_detect_silent_ignore`/field-coercion detectors match the exact minting
  signature rather than fuzzy-matching free text, and a belief with no matching
  detector reports zero savings with an explicit note rather than an invented
  estimate. `_tokens_per_call` guards its own division by zero.
- **`bench/ab.py`'s guard-report merging across arms is correct**: it sums `fired`
  counts from the actual `guards` list used in the shielded arm (`collect=used`)
  rather than reporting a freshly compiled, all-zero set, which would have silently
  under-reported guard activity.

## Resolution log

The findings above are the record of what the audit found; they are not edited
after the fact. This section records what was done about each one, and the
negative control that proves the fix is real rather than described.

| finding | status | negative control |
| --- | --- | --- |
| LOW `tests/test_invariants.py` non-strict xfail | fixed | The marker is removed and the test asserts the invariant that now holds (`_apply` drops the verdict and records `probe_uninformative`). Disabling the guard in `reflect/probe.py::_apply` makes the test fail; restoring it makes it pass. The suite reports a normal pass, not `1 xpassed`. |
| HIGH `reflect/templates.py::_consistency` status-blind create | fixed in `b71e343`, now locked down | `test_consistency_502_after_commit_cannot_confirm_silent_coercion` drives a scripted 502 through the template: the facts carry `usable: False` and no `mismatches` key, `_uninformative` is True, and the item is never read back. Disabling the status gate makes it fail. A paired test proves the gate did not blind the template to real coercion. |
| HIGH `reflect/templates.py::_boundary` create branch | fixed in `b71e343`, extended | Per-row `usable: False` covers one bad create, but a sweep where *every* create failed carries no `returned` key, so `_facts_uninformative`'s all-zero check had nothing to inspect and the empty sweep passed as a measurement. The template now reports `usable_rows` and sets `error` when that count is zero. Negative control: a sweep with one surviving create still reports its truncation. |
| LOW `reflect/templates.py::_timing` status-blind reads | fixed | `n0 != n1` was True whenever exactly one read was rate-limited, so a 429 read as eventual consistency. The template now gates on both statuses, reports `changed: None` and sets `error` otherwise. Negative control: two successful reads that genuinely differ still report `changed: True, delta: 2`. |
| MEDIUM `bench/settle.py` concurrency model | fixed | The per-adapter `min_interval` is replaced by one `RateBudget` shared by every adapter in the run; `header_burst`, whose measurement IS tripping the limiter, takes the budget exclusively rather than bypassing it so its burst cannot manufacture a sibling's 429. Negative control, four workers x eight calls against an isolated lab: 23/32 429s (71%) before, 0/32 (0%) after, elapsed 2.8s -> 10.6s. A unit test asserts no 1s window ever holds more than 3 sends, and a second asserts the budget does not block a fleet already under the limit. |
