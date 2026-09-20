# Evaluation

Every number here is produced by `make evaluate`, offline, with no network, no
credentials and no model calls. Run it yourself:

```bash
UV_PROJECT_ENVIRONMENT=.venv uv sync --frozen --group dev
make offline
```

The evaluation manifest is frozen before anything is measured: the commit, the
Python version, the lockfile hash, and the content hash of every fixture's raw
capture, documentation and traffic. A number in this record traces to the exact
inputs that produced it, and if the corpus changes the hashes change, so an old
record is visibly about something else rather than quietly comparable.

Environment for the run below: Python 3.11.16, Darwin arm64, `uv.lock` pinned,
commit recorded in `evaluation.json`.

---

## The corpus

10 fixtures across 8 tools, 54 recorded exchanges.

| | |
| --- | --- |
| documented claims under test | 20 |
| confirmed mismatches | 11 |
| claims the tool honours (controls) | 7 |
| claims the recording cannot settle (abstentions) | 2 |
| fixtures verifying against manifest, hashes, and re-derivation from raw | 10 / 10 |

Seven of the twenty claims are tools behaving exactly as documented. They are
there so precision means something: a checker eager enough to find a mismatch
everywhere would find one in those too, and every one of those would be false.

### Coverage by mismatch class

| class | fixtures | example |
| --- | --- | --- |
| status-code-mismatch | 1 | REST Countries returns **HTTP 200** carrying `success: false, data: null` |
| field-omission | 2 | Frankfurter v1 drops an unknown currency from `symbols` with no error |
| renamed-field | 1 | Frankfurter renames the filter `symbols` → `quotes` between v1 and v2 |
| rate-limit | 1 | the lab's undocumented 3 req/s, with `Retry-After` on only 5 of 7 rejections |
| pagination-cursor | 3 | GitHub silently clamps `per_page=200` to 100 |
| semantic-type-correct | 3 | Open-Meteo's error says `Given 16` whatever you actually sent |
| no-mismatch-control | 6 | MediaWiki performs the same clamp and **says so** in the response |
| unobservable | 2 | GitHub's `x-ratelimit-reset` names a time an hour away |

The sharpest thing in the corpus is that three tools clamp a page-size
parameter identically and only two of them are mismatches. MediaWiki clamps
`srlimit=5000` to 500 and returns a `warnings` object naming the parameter, the
offending value and the permitted range. The clamp is the same; the honesty is
not. A checker that fired on all three would be detecting clamping rather than
dishonesty, and the Wikipedia fixture is what stops that.

---

## Probe-selection policies

Four arms, identical fixtures, claims, budgets, success definition and seeds.
Only the choice of which exchange to buy next differs. 50 seeds for the
stochastic arm; the deterministic arms ignore the seed, so each is run once
rather than being given a fake distribution.

| arm | success | calls/run | sd | false beliefs | abstentions |
| --- | --- | --- | --- | --- | --- |
| fixed (capture order) | 1.000 | 4.30 | 2.06 | 0 | 2 |
| random (uniform) | 1.000 | 3.46 | 1.88 | 0 | 100 |
| **greedy (current)** | **1.000** | **2.90** | 1.79 | **0** | 2 |
| expected information gain | 1.000 | 3.90 | 1.20 | 0 | 2 |

**Expected information gain lost.** It spends 3.90 calls per run against
greedy's 2.90 - 34% more - at identical success and identical false-belief
count. The simpler policy is retained, and the result is asserted in
`tests/test_policies.py` so it cannot quietly stop being true: if a future
change makes EIG win, that test fails and this document has to be rewritten
rather than drifting.

Greedy does beat both naive baselines (2.90 against 4.30 fixed and 3.46
random), so the selector is doing work - it is the *entropy formula* that
failed to add anything, not selection in general.

### What the policy arms may see

A policy reads the request of a candidate probe, the documentation
(`operation`, `parameter`, `doc_claims`), and every response already paid for.
It cannot read an unbought response, and it cannot read `truth` or
`mismatch_class`. This is enforced structurally - `PolicyView` has no field
carrying an answer, and a test inspects the source of `bench/policies.py` for a
route to the labels. An EIG policy fitted on the evaluation labels would win by
construction and would have measured nothing but its own access to the answers.

---

## Contract drift

Frankfurter published v1 and v2 of the same endpoint. Both were captured on the
same day, minutes apart, so this is a real contract change rather than a
planted one. Four windows, the same rule set evaluated at every window.

| | |
| --- | --- |
| contract versions inferred | 2 |
| documentation changes | 1 |
| schema changes | 1 |
| semantic changes | 5 |
| transient failures promoted to a contract | 0 |

**Belief demotion**, measured rather than asserted:

| belief held under v1 | series across windows | outcome |
| --- | --- | --- |
| `non_publication_date_silently_substituted` | contradicts → contradicts → **supports** → supports | refuted after 2 windows |
| `unknown_symbol_silently_dropped` | contradicts → contradicts → **unobservable** → unobservable | set aside after 2 windows |

The second row is the one worth reading twice. v2 renamed `symbols` to
`quotes`, so the v2 recording never asks the question that belief answers. The
honest outcome is abstention, not refutation - recording it as "disproved"
would be inventing evidence from an absence, which is the error this project
has found in its own code five times. Both outcomes are kept.

---

## Guards

Every confirmed mismatch compiles to a runtime guard carrying the rule it came
from, the exchange keys that established it, and the content hashes of the
documentation and traffic it was derived from.

| | |
| --- | --- |
| guards compiled | 11 (9 distinct) |
| confirmed mismatches with no guard | 0 |
| guards firing on honest fixtures (false alarms) | 0 |
| guards firing on the evidence that created them | 11 / 11 |
| guards rejecting a planted violation | 9 / 9 |

An abstention compiles no guard and a satisfied claim compiles no guard.
Enforcing a rule nobody established is how "we could not tell" becomes "we
know", and both directions are tested directly.

---

## Mutation testing

A passing suite says nothing about whether the tests can fail. `make mutations`
restores each old bug in a temporary copy of the tree and checks that the named
test dies.

**25 mutations, 25 killed**, across 12 defect classes: vacuous-measurement (4),
drift-detection (4), corpus-integrity (3), rate-limit (3), status-blindness (2),
abstention (2), guard-precision (2), and one each for unknown-field, pagination,
wasted-call, side-effect and evaluation.

A mutation whose anchor no longer matches the source is reported `NOT APPLIED`
rather than skipped, because a green run full of unapplied mutations is exactly
the false comfort this file exists to prevent.

---

## Null results, losing results, and untested claims

Kept because dropping them would make the rest less trustworthy, not more.

- **Expected information gain does not beat the greedy selector** on this
  corpus: 3.90 calls/run against 2.90. The simpler policy is retained.
- **Probe cost is untested.** Every recorded exchange costs exactly one call,
  so the cost term in the EIG score is constant across candidates and the
  comparison measures information only. A corpus with uneven probe costs would
  test more of the policy than this one does.
- **Tokens and wall time are not measured.** Replay spends neither. Both are
  reported as `0` rather than estimated.
- **Two documented promises cannot be checked at all** and are recorded as
  abstentions rather than quietly dropped.
- **Licence boundaries are recorded as stated by each source, not as verified.**
  Each manifest carries `verified_from_terms_page: false` where the terms page
  was not independently parsed during capture.
- The historical headline - equal task success with 35% fewer tool calls, 37%
  fewer tokens and 71% lower wall time - **is not reproduced here.** It came
  from the live A/B against the lab with real model calls, and nothing in this
  offline package re-establishes it. It remains historical until re-run.
- The live-lab boundary likewise stands where it stood: precision 0.83, zero
  false beliefs, observable recall 0.38. Those are `bench/score.py` numbers
  against the lab's answer key, unchanged by this work.

---

## Independent verification

The checks above were re-run in a checkout built from the tracked files alone:
no virtualenv, no run logs, no `evaluation.json`, dependencies installed from
`uv.lock` only. Everything reproduced identically.

| | this tree | independent checkout |
| --- | --- | --- |
| tests | 254 passed | 254 passed |
| fixtures verifying | 10 / 10 | 10 / 10 |
| regression gate | pass | pass |
| policy calls/run (fixed / random / greedy / eig) | 4.30 / 3.46 / 2.90 / 3.90 | 4.30 / 3.46 / 2.90 / 3.90 |
| contract versions from the drift timeline | 2 | 2 |
| mutations killed | 25 / 25 | 25 / 25 |

This is the property the counterfactual replay never had. That number was
computed from `runs/*.jsonl`, which is gitignored, so its input was untracked
local state: the published figure drifted from 115 distinct wasted calls to
117 with nothing able to notice, and on a fresh clone the command correctly
reports zero. Everything in this document is computed from committed bytes
instead.

## Reproducing

```bash
UV_PROJECT_ENVIRONMENT=.venv uv sync --frozen --group dev
make offline          # suite, corpus replay, gate, policies, drift, record
make mutations        # slower: ~24 subprocess pytest runs
```

`make capture` re-records the fixtures from live services. It is the only
command here that needs a network, and it refuses to overwrite an existing raw
capture: a correction creates a new version rather than rewriting the record an
earlier measurement was taken against.
