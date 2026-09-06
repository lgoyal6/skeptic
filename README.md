# skeptic

An agent that reverse-engineers its own tools.

> Every agent is handed tool documentation and told to trust it. This one doesn't. It runs experiments on its tools, learns what they actually do, discards beliefs that stop being true, and publishes the spec so the next agent doesn't have to.

Built in about 30 hours for "Syndicate by Maximor", a hackathon hosted by AO (Agent Orchestrator), Track 1: Automated Agent Engineering. The organisers said they would judge on: whether the agent gets better over time; whether that improvement comes from its own self-reflection and growing memory, rather than luck; whether it learns contextual logic from third-party tool data and applies it later; and whether it balances cost-effectiveness and speed.

skeptic is pointed at a small HTTP API (`lab/`) whose documentation is confidently written and wrong in fourteen specific, previously-known places. The agent is never shown the answer key. It has to notice the documentation is lying, work out several rival explanations for what it actually observed, design an experiment that would tell those explanations apart, commit to a prediction before running it, and only then decide what it believes. What it ends up believing is written down as a YAML file with a posterior probability attached, not a sentence it is proud of. When the underlying tool changes behaviour later, the same posterior can walk a belief back to "unlearned" without anyone deleting anything by hand.

The point of the exercise is not the toy API. It is that "trust the documentation" is the default failure mode for every agent that calls a real tool, and this is what refusing to do that looks like in practice, all the way through to a corrected spec another agent can load instead of the vendor's docs.

## How it works

The loop, in order:

```
recon                    deliberately test every promise lab/DOCS.md makes
  |                       (12 of the 14 documented claims, cheapest-first)
  v
ContractLayer.record()   deterministic, no LLM: doc spec vs. what came back
  |                       on the wire -> Anomaly, or nothing
  v
propose_batch()          ALL anomalies in ONE reflector call -> 2-3 rival
  |                       hypotheses per anomaly, each with a stated
  |                       "distinguishing test"
  v
probe.design()           pick the experiment template that separates the
  |                       most rival classes per call (greedy score, not
  |                       formal expected-information-gain)
  |
  |   commit predictions BEFORE running -- this is the part that matters
  v
run the experiment       reflect/templates.py: boundary sweep, timing,
  |                       idempotency, consistency, ordering, header burst,
  |                       flag discovery
  v
verdict                  strict: "consistent with" is not "confirmed"; an
  |                       uninformative observation (empty vendor, a sweep
  |                       that saw nothing) may confirm or falsify NOTHING
  v
confirm / falsify / -----+--- all rivals refuted -> reflect/reopen.py:
  inconclusive            |    re-propose using the refutation itself as
  |                       |    evidence, rather than giving up on the anomaly
  v
Belief.posterior          a beta-binomial over "the documentation is
  (agent/beliefs.py)       correct"; every later observation, in either
  |                        direction, can move it back across the trust
  |                        threshold and auto-falsify a belief that used
  |                        to be true
  v
shim/export.py            confirmed beliefs -> export/TOOLS.md (corrected
                           spec) + export/guards.json (the same beliefs
                           compiled into enforcement code, shim/guards.py)
```

Detection is deliberately not an LLM call. `agent/contract.py` holds `lab/DOCS.md`'s promises as a data structure and checks the wire against it; the same run produces the same anomalies every time, which is what makes `make bench SEED=n` a reproducible number rather than a vibe. The model's job starts after an anomaly exists: explaining it, not spotting it.

Hypothesis generation is batched (`reflect/batch.py`): recon can surface a dozen anomalies at once, and the first version of this called the reflector once per anomaly. One call for the whole batch is both cheaper and, per the code's own reasoning, produces better hypotheses, because a model that sees "titles truncate" and "bulk silently drops rows" in the same view can notice they are the same class of defect.

Selection of which experiment to run is a **greedy discrimination score** (`reflect/templates.py:score_template`): rival classes separated, divided by estimated calls. That is explicitly not a formal expected-information-gain calculation; the code's own docstring says a greedy discriminator that works is worth more than an entropy formula nobody can verify from a three-minute demo video.

**Two-tier model split.** `agent/llm.py` defines an `executor` role (every task step, every tool call, cheap and fast) and a `reflector` role (hypothesis generation, probe design, verdicts, called a handful of times per run, not per step). The intent, per the code's fallback chain, is GLM-4.7-Flash (via TensorMux) for the executor and a stronger model (GPT-5 Nano, if an `AIGRANTS_API_KEY` is present) for the reflector. In the environment this build actually ran in, only `TENSORMUX_API_KEY` is configured, so the reflector fell back to the same GLM-4.7-Flash endpoint as the executor (`agent/llm.py:build()`'s own fallback path). The role split, and its separate usage accounting (`Usage.by_role`), is real and is what the cost story rests on; the model-strength asymmetry described in `ao/WORKERS.md` (a stronger adversary model reviewing a weaker proposer) is the same mechanism applied to the AO falsifier worker, described below, which does run on a different harness (Claude Code) than the GLM-4.7-Flash proposer.

## The lab

`lab/server.py` is a small FastAPI service with fourteen deliberate discrepancies between what `lab/DOCS.md` says and what the server does; `lab/ground_truth.yaml` is the answer key, and the agent never reads it. `bench/score.py` matches a confirmed belief to a ground-truth rule structurally, by class, operation and parameter, not by asking an LLM whether two sentences mean the same thing, because an LLM judge grading its own sibling's homework is exactly how a benchmark becomes fiction.

| id | class | operation / parameter | docs claim | actually |
| --- | --- | --- | --- | --- |
| `page_size_cap` | silent_truncation | search / page_size | accepts 1-500 | clamped to 50; `has_more=true`, no warning |
| `rate_limit_flaky_header` | rate_limit | * | no documented rate limit | 3 req/s; `Retry-After` present on only ~60% of 429s |
| `write_search_lag` | eventual_consistency | search | items are immediately searchable | invisible to search for ~2000ms after creation |
| `unknown_filter_field` | silent_coercion | search / filter | unknown field -> 400 invalid_filter | 200 with an empty result set, no error |
| `pre_epoch_date_null` | silent_coercion | create / due_date | any ISO-8601 date | dates before 1970-01-01 silently stored as null |
| `large_number_to_string` | silent_coercion | create / amount | always a JSON number | amounts > 1,000,000 serialised as a string |
| `cursor_expiry` | expiry | search / cursor | valid indefinitely | expires 60s after issue; 400 cursor_expired |
| `include_archived_flag` | undocumented_flag | search / include_archived | not documented | archived items hidden unless this undocumented flag is set |
| `unknown_field_ignored` | silent_coercion | update / * | unknown field -> 400 unknown_field | 200, field silently discarded |
| `archived_get_404` | undocumented_flag | get | returns the item regardless of archived state | 404 for an archived item |
| `bulk_cap` | silent_truncation | bulk_create / items | up to 100 items | only the first 20 processed; reports success for all |
| `sort_created_is_edited` | mislabelled_semantics | search / sort | `created` orders by creation time | orders by last_edited time |
| `title_truncation` | silent_truncation | create / title | up to 2000 characters | silently truncated to 255 |
| `write_502_after_commit` | idempotency_hazard | create | 502 means the request was not processed | ~5% of creates 502 *after* committing; a naive retry duplicates |

Recon's sweep (`reflect/recon.py`) directly tests 12 of these 14 documented promises; `include_archived_flag` and `write_502_after_commit` are not promises the docs make explicitly (one is entirely undocumented, the other is a claim about a status code's meaning), so they surface instead through task exposure and the `idempotency` probe template.

## Results

Numbers below were read directly from the artifacts named, or produced by running the exact commands in the task brief, moments before this was written. `beliefs/lab.yaml` was last updated `2026-09-06T03:49:21Z`; two other artifacts (`export/TOOLS.md`, `export/guards.json`) are visibly older than that and are flagged as stale below rather than quietly treated as current.

**`./.venv/bin/python cli.py bench`** (live):

| | |
| --- | --- |
| confirmed beliefs | 4 |
| matched a real rule | 4 |
| false beliefs | 0 |
| duplicates | 0 |
| precision | 1.00 |
| recall, observable rules | 0.31 (4/13) |
| recall, all 14 rules | 0.29 (4/14) |
| belief lifecycle | hypothesis 7, confirmed 4, falsified 16, retired 0 |

13 of the 14 ground-truth rules were observably triggered at some point in this session (`runs/observable.json`); `large_number_to_string` never fired, so it is excluded from the observable-recall denominator rather than counted as a miss.

The 4 confirmed, matched beliefs: `rate_limit_flaky_header`, `title_truncation`, `unknown_field_ignored`, `write_search_lag`. The 9 rules that were observable but not currently held as a confirmed belief: `archived_get_404`, `bulk_cap`, `cursor_expiry`, `include_archived_flag`, `page_size_cap`, `pre_epoch_date_null`, `sort_created_is_edited`, `unknown_filter_field`, `write_502_after_commit`, some of these are open hypotheses (7 currently unsettled), the rest were proposed at some point and falsified.

### The A/B: does the learned knowledge transfer?

Two fresh agents, neither with any memory. One gets the documentation. The
other gets the documentation plus the guards compiled from what skeptic
learned. Identical tasks, identical world, three repeats of each task per arm.

```
                            naive       shielded
                      (docs only)   (docs+guards)
    inventory                FAIL           FAIL
    oldest_three             FAIL           FAIL
    exactly_once             PASS           PASS
    ------------------------------------------------
    passed                    3/9            3/9
    tool calls                 66             43     -35%
    tokens                587,942        368,082     -37%
    wall seconds            473.7          136.1     -71%
```

**Read this honestly: there is no success-rate difference.** Both arms pass
three of nine. What the guards bought is cost, not accuracy: about a third
fewer tool calls, a third fewer tokens, and a large drop in wall time.

The reason is specific rather than mysterious. The two tasks that fail need
guards for beliefs the agent has not confirmed yet: `inventory` needs the
pagination cap and the undocumented archived flag, `oldest_three` needs the
sort-semantics rule. The guards that did compile (pacing, write-visibility,
schema rejection, title truncation) prevent wasted work rather than wrong
answers, so that is exactly what they show up as.

An earlier single run did show 0/3 against 1/3 and it was tempting to report.
It was noise: the 502-after-commit rule fires on roughly 5% of creates, so
whether the naive agent makes duplicates on `exactly_once` is a coin toss,
and across three repeats it passed every time. One run per cell could not have
distinguished a real effect from that, which is why the number above is nine
runs per arm and not three.

## How AO was used

AO is used as the orchestration layer around a command surface that is deliberately deterministic (`ao/ORCHESTRATOR.md`): `cli.py run`, `cli.py probe`, `bench.settle`, `bench.apply_probes`, `bench.ab`, `ao.falsify`, `shim.export` all do exactly the same thing given the same seed and belief store, so `make bench SEED=n` means something. AO's job is the judgement calls a deterministic script cannot make well, which command to run next, which open hypothesis is worth spending a probe on, never reaching into the measurement itself (hand-editing a probe verdict, writing `beliefs/*.yaml` by hand, calling the lab's HTTP API directly "to just check something"). `ORCHESTRATOR.md`'s own rule: "if you find yourself about to do the CLI's job by hand, stop and run the CLI instead."

The asymmetry that matters: every belief in the store was produced by the same reflector (GLM-4.7-Flash) that wanted its own hypothesis to be true. A confirmation from that same model is the author marking their own homework, and that failure mode already showed up once, when a hypothesis predicted correctly while asserting an untested cause ("capped at 50 because of your subscription tier"). So the falsifier is spawned as a separate `ao spawn` worker running on Claude Code, a different, stronger harness than the GLM-4.7-Flash proposer, with an inverted brief: not "is this belief supported" but "design the experiment most likely to break it" (`ao/falsify.py`, `ao/WORKERS.md`). A review of `"sound"` sets `survived_falsification`; anything else (`overgeneralised`, `unsupported_cause`, `wrong`) demotes the belief back to a hypothesis and records the adversary's proposed attack as the next experiment to try. Two other worker roles are specified the same way: a `prober` (codex) that runs `bench.settle` in its own worktree so probes do not block the orchestrator session, and a `reporter` (claude-code) that regenerates `export/TOOLS.md` / `export/guards.json` and summarises what changed.

Events flow back through `ao/eventd.py`, which reads AO's SSE stream at `/api/v1/events` with `Last-Event-ID` replay (a restart resumes rather than losing events), filters to the events actually worth waking the orchestrator for (`session.completed`, `session.needs_input`, `session.failed`, PR lifecycle events), and forwards each as a message telling the orchestrator to decide its next action. Worker output is read back directly from `~/.ao/data/worktrees/skeptic/<session-id>/`, not from a merged PR, `falsification.json` and `report.md` are scratch files the worker writes as its last act, polled from the filesystem, because they exist to talk back to the orchestrator, not to be part of the published history. `--permission accept-edits` (`ao project set-config skeptic --permission accept-edits`) was necessary because, without it, a spawned worker sat at `needs_input` indefinitely the first time this was tried, accept-edits is what lets a worker actually finish its assigned, narrow task without a human in the loop for every file write.

This snapshot's evidence for the mechanism actually running: two AO worktrees exist on disk (`~/.ao/data/worktrees/skeptic/skeptic-1`, `skeptic-2`), one containing a small connectivity-check file (`ao_probe_test.json`, `{"ok": true, "who": "claude-code"}`) confirming a Claude Code worker was successfully spawned and could write back to its worktree. Neither worktree contains a completed `falsification.json` or `report.md` from a full falsifier/reporter run, so the specific verdicts a live falsifier worker produced are not something this README can quote as a number; the mechanism described above is what the code implements and what was wired up to run, not a transcript of one particular adversarial session.

## What was found by running it

The single strongest finding is that **the same measurement bug appeared five times**: asserting something without first establishing that it could have been observed. `tests/test_invariants.py` documents this directly:

1. A lab smoke test asserted `page_size <= 50` against a 25-row store, the cap was never exercised, so the assertion passed vacuously.
2. A detector test asserted on anomaly *kind* rather than the exact error, `cursor_expiry` "passed" because that kind appeared elsewhere in the run while nothing had actually fired for the cursor. Fixed by requiring the exact `cursor_expired` error string (`agent/contract.py`), not a substring match.
3. Recon's own page-size check ran against a 20-row vendor, the same vacuous pass, one layer up. Fixed by `reflect/templates.py:_ensure_population()`, which pads a vendor to at least ~60 rows before a boundary sweep runs.
4. Worst: a probe swept a vendor with **zero** rows, got 0 results for every value, and the verdict read that as *refuting a belief that was true*. An experiment with no signal destroyed a correct belief. Fixed by `_uninformative()` / `_facts_uninformative()` checks that force every verdict on an empty observation to "inconclusive".
5. The fix for #4 originally lived only in `reflect/probe.py:run_probe()`, upstream of `_apply()`. But `bench/settle.py` and `bench/apply_probes.py` both call `_apply()` directly on probe records loaded back from disk (the durability path for surviving a crashed settle run), bypassing that upstream guard entirely, a record with an empty sweep could still falsify a true belief on re-application. This was caught by the invariant test suite and fixed by moving the check into `_apply()` itself, at the point of effect rather than at one caller. The evidence this is genuinely fixed, and not just described as fixed: `tests/test_apply_alone_does_not_guard_against_uninformative_observation` is still marked `xfail(strict=False)` with a docstring describing the gap as open, but the live test run reports it **1 xpassed**, the assertion now holds, because the guard was moved after the test was written, and the test's own comment is now stale. A project about documentation that lies has a test whose comment is, at this moment, one of the lies.

A related but distinct bug: the reflector once labelled an `update`/`silent_ignore` anomaly (a field silently discarded on PATCH) as `idempotency_hazard`, a different defect class entirely, plausible-sounding prose with no support in the wire evidence, and it scored as a false belief. Fixed by `reflect/hypothesis.py:constrain_class()`, which restricts the class a hypothesis may be minted with to whatever the anomaly's own `kind` actually admits, regardless of what the model wrote.

Two smaller, project-appropriate ironies: the `Makefile` used to advertise `bench`, `ab` and `export` targets it did not define, documentation lying about its own tool, inside a project about documentation that lies, and is now honest (every target `help` lists is defined; see Running it, below). And there is no `skeptic` console binary despite `pyproject.toml` declaring one (`[project.scripts] skeptic = "cli:main"`), because `[tool.uv] package = false` means it was never built; every command in this README is the real, runnable `./.venv/bin/python cli.py ...` / `./.venv/bin/python -m ...` form.

## Claim boundaries

What this does and does not establish, stated plainly because the reader should not have to dig for it:

- **Novel application, not novel mechanism.** Active experimentation, competing hypotheses, and belief revision under a posterior are not new ideas. The contribution is pointing that loop at reverse-engineering undocumented tool behaviour specifically, with a scoreable answer key, rather than inventing a new learning algorithm.
- **Scored numbers come from the instrumented lab**, whose ground truth (`lab/ground_truth.yaml`) skeptic itself controls and the agent never sees. This proves the loop works when the ground truth is knowable in advance. It does not, by itself, prove the loop generalises to an API skeptic did not author, whose docs were written by someone else. The Notion adapter (`agent/adapters/notion.py`) exists specifically to test that generalisation, same call shape, same ContractLayer wiring, writes disabled by default, but any result against the live Notion API is qualitative: there is no answer key for a third party's real production service, so nothing about Notion can be reported as a precision/recall number the way the lab results above can.
- **Experiment selection is a greedy discrimination score** (rival classes separated per estimated call), explicitly **not** a formal expected-information-gain calculation. The code's own docstring in `reflect/templates.py` says as much: a greedy heuristic that measurably works is worth more here than an entropy formula nobody watching a three-minute demo could verify.
- **The A/B and the ablation are two different kinds of evidence.** The A/B (above) shows the compiled guards changing an outcome for a fresh, memory-less agent, knowledge transferring rather than living only in one belief file. The memory-wipe ablation (`bench/ablate.py`) is designed as the stronger control (memory vs. no memory vs. a token-length-matched but *shuffled* belief store, to separate "the knowledge helped" from "a longer prompt helped"), but a full run did not complete in this snapshot: `.agent-work/ablate_run.log` shows a partial run (5 confirmed beliefs at the time; memory arm 1/3 tasks passed; the wiped arm's third task and the entire shuffled arm are not in the log) and no `bench/ablate_result.json` exists on disk. The ablation's design is real and reviewed above; its completed numbers are not available in this repo snapshot and are listed under "numbers still to fill" rather than invented.

## Running it

Every target below is read directly from the `Makefile` and is genuinely defined (the point of the fix described above).

```
setup
  make lab            start the instrumented lab on :8077
  make lab-stop       stop it
  make smoke          prove all 14 hidden rules fire on the wire
  make detect         prove the contract layer sees all 14

the loop
  make recon          sweep the documented promises, mint hypotheses
  make settle         settle open hypotheses (concurrent probes)
  make apply          apply saved probe verdicts to the belief store
  make learn          recon + settle + apply, end to end
  make run            one task run against the lab

evidence
  make bench          score beliefs against ground truth
  make status         what is believed, and how sure
  make export         emit TOOLS.md + the guard manifest
  make ab             naive agent vs shim-equipped agent
  make ablate         memory / wiped / shuffled control
  make retire         watch a belief unlearn when the world changes
  make replay         what would these beliefs have saved?
  make verify         reproduce every number in the README

  make ui             serve the panels for `ao preview`
```

`make verify` runs `smoke`, `detect`, `bench` and `replay` in sequence, everything that is cheap and does not spend a model call. The A/B, the ablation and the retirement demo each cost real model calls, so they are separate targets (`make ab`, `make ablate`, `make retire`) rather than folded into `verify`.

## Repo layout

```
lab/       the instrumented tool: server, its (wrong) docs, the answer key
agent/     contract layer, belief store, task suite, the two-tier LLM client,
           the executor loop, adapters (lab, notion)
reflect/   recon sweep, batched hypothesis generation, probe design/templates,
           reopening exhausted anomalies
shim/      guards derived from confirmed beliefs; export to TOOLS.md + guards.json
bench/     scoring, the A/B, the memory-wipe ablation, the retirement demo,
           settling hypotheses concurrently, applying saved probe records
replay/    counterfactual replay of run logs against learned beliefs
ao/        the orchestrator's operating manual, the three worker prompts,
           the SSE event bridge, the falsification worker
runs/      per-run JSONL logs (every call, every anomaly) and the observed-
           rules ledger
probes/    one JSON record per settled experiment: params, committed
           predictions, observation, verdict
beliefs/   the belief store (one YAML file per tool under test)
export/    the generated corrected spec and guard manifest
tests/     regression tests for the measurement machinery itself
ui/        panels for `ao preview`
```

## numbers still to fill

- **Ablation totals** (`bench/ablate.py` / `make ablate`): final passed/total for the `wiped` and `shuffled` arms, total tokens per arm, and the automated "reading" verdict. Only a partial, uncompleted log (`.agent-work/ablate_run.log`) exists in this snapshot; no `bench/ablate_result.json` is on disk.
- **A run-over-run learning curve** (task success rate as belief count grows): `runs/history.jsonl`, the file this would come from, does not exist in this repo snapshot, nothing has invoked `cli.py run` or `bench.session` this session. The belief lifecycle counts and the retirement trajectory above are the only over-time evidence currently available.
- **Live falsifier/prober/reporter output** from a completed AO worker run (a real `falsification.json` or `report.md`): the two AO worktrees present on disk contain only a connectivity check, not a completed adversarial review.
