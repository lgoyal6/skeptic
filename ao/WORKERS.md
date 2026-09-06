# WORKERS.md

Prompts for the three roles the orchestrator spawns as AO workers. Each one
runs in its own isolated git worktree at
`~/.ao/data/worktrees/skeptic/<session-id>`, on its own branch
(`ao/<session-id>/root`), never in the orchestrator's own working copy.

Two preconditions this installation needed, so spawning does not silently
hang:

- **Permission.** The project is set `ao project set-config skeptic --permission accept-edits`. Without it, a worker sat at `needs_input` indefinitely the first time this was tried. If you are setting this up fresh, do that before spawning anything below.
- **A resolvable default branch.** `ao spawn` needs one to create the worktree from. This repo has a local bare remote at `/tmp/skeptic-origin.git` purely so that resolves.

Session ids come back from `ao spawn`'s output as a token prefixed with the
project name, e.g. `skeptic-a1b2c3`; `ao/falsify.py:spawn()` parses it that
way rather than assuming a fixed format.

---

## falsifier (`claude-code`)

**Purpose.** The adversary. Every belief in the store was produced by the
same reflector (GLM-4.7-Flash) that wanted it to be true, a confirmation
from the model that proposed the hypothesis is the author marking their own
homework, and it has already gone wrong once here: a hypothesis predicted
correctly while claiming a cap depended on the API key's subscription tier,
a causal story nobody actually tested. The falsifier runs on a strictly
stronger model than the proposer, specifically so it does not share the
proposer's blind spots and rubber-stamp. Its brief is inverted: not "is this
belief supported" but "design the experiment most likely to break it".

This role already exists as code, not just as a doc convention:
`python -m ao.falsify --apply` performs the spawn, the wait, and the apply
below for you. What follows is exactly what that command does under the
hood.

**Spawn command** (as built by `ao.falsify.spawn()`):

```
ao spawn --project skeptic --kind worker --harness claude-code \
  --name falsifier --prompt "<BRIEF, filled in below>"
```

**Prompt text**, `BRIEF` from `ao/falsify.py`, with `{beliefs}` and
`{evidence}` filled in by `_fmt_beliefs(store)` (every currently-active
confirmed belief, plus, for each, the facts and learned text from its probe
record(s) on disk):

```
You are the adversary in a system that reverse-engineers APIs.

Another model observed an API, formed hypotheses about how it really behaves,
ran experiments, and recorded the beliefs below as CONFIRMED. It marked its
own homework. Your job is to find where it is wrong.

For each belief, ask specifically:

  1. Does the stated claim actually follow from the evidence cited, or does it
     assert a CAUSE that was never tested? "Capped at 50" follows from a
     sweep. "Capped at 50 because of your subscription tier" does not, unless
     tiers were varied.
  2. Is the claim over-general? A cap observed for one vendor is not a global
     cap until another vendor was tried.
  3. Is there a cheaper, more boring explanation that fits the same evidence?
  4. What single experiment would most likely BREAK this belief if it is
     wrong? Be concrete: which call, which parameters, which outcome would
     refute it.

BELIEFS UNDER REVIEW
{beliefs}

THE EVIDENCE EACH ONE RESTS ON
{evidence}

Write your answer to `falsification.json` in the repository root as a single
JSON object, then stop. Do not modify any other file.

{
  "reviews": [
    {
      "belief_id": "<id>",
      "verdict": "sound" | "overgeneralised" | "unsupported_cause" | "wrong",
      "attack": "<the one experiment most likely to break it, concretely>",
      "because": "<one sentence, citing the specific evidence gap>"
    }
  ]
}

Be harsh. A belief you cannot fault is fine, but say why you could not fault
it rather than agreeing by default.
```

**File it must write.** `falsification.json` at the repo root of its own
worktree, the single JSON object shown above.

**How the orchestrator reads it back.** `ao.falsify.await_output()` polls
`~/.ao/data/worktrees/skeptic/<session-id>/falsification.json` every 10s (up
to a 600s default timeout) and parses it as soon as it exists, a direct
filesystem read of the worker's worktree, not a PR merge, because this file
is scratch output meant only to talk back to the orchestrator, not part of
the published history. `--apply` then walks each review: `"sound"` sets
`survived_falsification = "adversary"` on that belief; anything else demotes
it from `confirmed` back to `hypothesis`, with the adversary's proposed
`attack` recorded in its history as the next experiment worth running. A
copy also lands at `probes/falsification.json` in the orchestrator's own
tree, so the review itself is kept as evidence.

---

## prober (`codex`)

**Purpose.** Settling every open hypothesis with `bench.settle` is I/O
bound, each probe spends a couple of seconds calling the lab and several
minutes waiting on the model, and can run for a while even with several
workers in parallel. Spawning it as its own AO worker means the orchestrator
session stays free to keep handling other events (a PR landing, a check
failing) while settling runs in the background, instead of blocking on it
directly.

**Spawn command:**

```
ao spawn --project skeptic --kind worker --harness codex \
  --name prober --prompt "<prompt, below>"
```

**Prompt text:**

```
From the repository root of this worktree, run:

  ./.venv/bin/python -u -m bench.settle --workers 3

Let it finish. It prints the number of rival groups it settled and, at the
end, a line of the form `beliefs: {'hypothesis': N, 'confirmed': N,
'falsified': N, 'retired': N}`, that line is your report. Do not edit
beliefs/lab.yaml or any file under probes/ by hand; bench.settle writes both
as a side effect of the experiments it runs. Then stop.
```

**File it must write.** None beyond what `bench.settle` already produces as
its normal side effect: updated entries in this worktree's own
`beliefs/lab.yaml`, and one new file per settled group under this worktree's
own `probes/*.json`. Because this worktree is an isolated git checkout, none
of that touches the orchestrator's working copy directly.

**How the orchestrator reads the result back.** Two options, and they serve
different purposes:

- **A quick peek**, read `~/.ao/data/worktrees/skeptic/<session-id>/beliefs/lab.yaml` and the new files under `.../probes/` directly from disk, the same technique `ao.falsify.await_output()` uses for a single file, generalized to a directory. Good enough to decide what to do next.
- **Making it authoritative**, the new probe records and belief changes only become part of the published history once they are merged into the main line. Once `session.completed` fires, check the PR (`ao review trigger`, then `ao pr merge` once checks are green) so the next `cli.py bench` and `shim.export`, run from the orchestrator's own worktree, actually see the new evidence.

`ao session get <session-id> --json` is worth checking alongside the raw
file poll: `bench.settle` has no single "done" marker file the way the
falsifier does, so confirming the session itself has reached a terminal
state is the more reliable signal that the belief file has stopped changing.

---

## reporter (`claude-code`)

**Purpose.** Keep the published spec current without making anyone diff
`beliefs/lab.yaml` by hand to find out what changed. Regenerates
`export/TOOLS.md` and `export/guards.json`, and writes a short human summary
of what moved since the last export.

**Spawn command:**

```
ao spawn --project skeptic --kind worker --harness claude-code \
  --name reporter --prompt "<prompt, below>"
```

**Prompt text:**

```
From the repository root of this worktree:

1. Note what export/TOOLS.md currently says (it was checked out from the
   branch you started on).
2. Run:

     ./.venv/bin/python -m shim.export

   This regenerates export/TOOLS.md and export/guards.json from the current
   beliefs/lab.yaml.
3. Compare the new export/TOOLS.md against what it said before you ran the
   export. Write a short summary (5-10 sentences, plain prose, no marketing
   language) of what changed -- which classes of belief are newly confirmed,
   which guards are newly compiled, which beliefs were retired or demoted --
   to a file named report.md at the repository root.
4. Stop. Do not touch any file other than export/TOOLS.md, export/guards.json,
   and report.md.
```

**File it must write.** `report.md` (the summary) at the repo root of its
worktree, plus the regenerated `export/TOOLS.md` and `export/guards.json` as
ordinary tracked-file changes on its branch.

**How the orchestrator reads it back.** Poll
`~/.ao/data/worktrees/skeptic/<session-id>/report.md` the same way as the
falsifier's output file, to get the summary as soon as it exists. Unlike
`falsification.json`, `export/TOOLS.md` and `export/guards.json` are meant
to be the published artifact, not scratch, so once `session.completed`
fires and the summary reads as expected, trigger `ao review trigger` and
land it with `ao pr merge` so the regenerated spec becomes what the main
worktree actually has on disk.
