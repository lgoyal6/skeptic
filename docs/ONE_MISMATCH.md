# One mismatch, end to end

The whole loop on a single real defect, from the recorded call that shows it to
the guard that refuses it, with the command to reproduce each step. Nothing
here needs a network, a credential, or a model call.

The defect: **Frankfurter v1 returns a different day's exchange rates than the
day you asked for, under HTTP 200, with every field type-correct.**

---

## 1. What the documentation promises

`fixtures/frankfurter/v1-2026-09-19/docs.md`, captured alongside the traffic so
the claim is judged against the docs as they were, not as they are today:

> A dated request returns the rates for that date, echoed in a `date` field.
> Rates come from central bank publications, which publish on working days.

A caller is entitled to conclude that asking for 2026-09-13 returns 2026-09-13.

## 2. The observation

```bash
./.venv/bin/python -m bench.replay_corpus --tool frankfurter --version v1-2026-09-19
```

Two of the five recorded exchanges:

| request | status | `date` in body |
| --- | --- | --- |
| `GET /v1/2026-09-11?base=USD&symbols=EUR` | 200 | `2026-09-11` |
| `GET /v1/2026-09-13?base=USD&symbols=EUR` | 200 | **`2026-09-11`** |

2026-09-13 is a Sunday. No rate was published, so the API substituted Friday's
and returned it under a success status. Nothing is malformed: the status is
200, `date` is a valid ISO date, `rates.EUR` is a float. A client that reads
`rates` without comparing `date` to its own request silently books Friday's
rate as Sunday's.

## 3. The check, and what it refuses to conclude

`fixtures/checks.py::non_publication_date_silently_substituted` returns one of
three verdicts, and the third is the important one:

```bash
./.venv/bin/python -c "
from fixtures.checks import evaluate, load_traffic
rows = load_traffic('frankfurter','v1-2026-09-19')
print(evaluate('non_publication_date_silently_substituted', rows).outcome)"
# contradicts_doc
```

Feed it **only** the Friday exchange and it does not say the documentation
holds:

```bash
./.venv/bin/python -c "
from fixtures.checks import evaluate, load_traffic
rows = [r for r in load_traffic('frankfurter','v1-2026-09-19')
        if r['path'].endswith('2026-09-11')]
v = evaluate('non_publication_date_silently_substituted', rows)
print(v.outcome); print(v.detail)"
# unobservable
# every dated request observed (['2026-09-11']) was for a publication day, which
# could not have been substituted. The evidence matches the documentation but was
# never capable of contradicting it
```

A Friday coming back as that Friday proves nothing: a publication day could not
have been substituted. Concluding "the docs are honest" from it is a vacuous
pass - the same shape as asserting a page-size cap against a store too small to
reach it, which is the first bug this project ever found in itself.

This was not caught by reading the code. The four-arm policy comparison buys
exchanges one at a time, and that partial-evidence view produced **52 false
beliefs** across the arms, every one of this shape. Fixing the class took the
count to **0**.

## 4. The belief, and its demotion when the world changed

Frankfurter shipped v2. The same rule, re-asked at every window:

```bash
./.venv/bin/python -m bench.drift_demo
```

```
non_publication_date_silently_substituted
    contradicts_doc -> contradicts_doc -> supports_doc -> supports_doc
    demoted after 2 window(s), to 'supports_doc'
```

v2 returns 2026-09-13 for 2026-09-13. The belief was true and is now false, and
the change is *measured* from recorded evidence rather than inferred from a
version number. The v1 contract is not overwritten - it stays inspectable and
replayable as version 1, because it is the only record that the change
happened.

Note what happens to the neighbouring belief. v2 renamed `symbols` to `quotes`,
so the v2 recording never asks the question `unknown_symbol_silently_dropped`
answers, and that belief goes to `unobservable` rather than `supports_doc`. It
is set aside, not refuted. Recording it as disproved would be inventing
evidence from an absence.

## 5. The guard

```bash
./.venv/bin/python -m bench.ci
```

The confirmed mismatch compiles to `assert_echoed_date_matches_request`,
carrying its provenance:

```
assert_echoed_date_matches_request   semantic-type-correct  frankfurter@v1-2026-09-19
    rule=non_publication_date_silently_substituted
    evidence=['0f3ab1f694343bc738d3c50c']
    traffic=sha256:c0a2c14b8c23...
```

At runtime it refuses the response:

> requested date 2026-09-13 and the response carries 2026-09-11: a different
> day's data returned under a 200, with every field type-correct

The gate then checks three things about it. It stays silent across every
honest fixture in the corpus. It fires on the exchange that created it. And it
rejects a hand-written planted violation, so a guard whose recorded case
happens to be easy is still exercised.

## 6. The regression test

```bash
./.venv/bin/python -m bench.mutations --only date_check_requires_a_date_at_risk
```

Restores the vacuous-pass behaviour in a temporary copy of the tree and
confirms the test dies:

```
date_check_requires_a_date_at_risk vacuous-measurement  killed (test failed)
```

---

## The loop, in one line each

| step | artefact | command |
| --- | --- | --- |
| documentation | `fixtures/frankfurter/v1-*/docs.md` | - |
| observation | `fixtures/raw/frankfurter/v1-*/capture.jsonl` (immutable) | `make replay-corpus` |
| verdict | `fixtures/checks.py` → `contradicts_doc` + evidence keys | - |
| contract | `contracts/` → version 1, predecessor preserved | `make drift` |
| demotion | measured over 4 windows when v2 arrives | `make drift` |
| guard | `shim/corpus_guards.py` → `assert_echoed_date_matches_request` | `make gate` |
| regression | mutation kills the test that protects it | `make mutations` |
