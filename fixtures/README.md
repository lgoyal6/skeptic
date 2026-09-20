# fixtures: replay-first tool recordings

Every number this project publishes about a tool has to be reproducible by
somebody who is not sitting at the machine that produced it. The counterfactual
replay was not: `runs/*.jsonl` is gitignored, so its input was untracked local
state, and the figure in the README drifted from 115 distinct wasted calls to
117 without anything being able to notice. A measurement whose input is not in
the repository is a property of one laptop.

So: **live services are used once, to create a redacted recording. Tests and
benchmarks run from the recording.**

That also settles a second problem. Skeptic's method is to design experiments
against a tool and see whether its documentation is honest. Run that against
somebody else's production API on every test run and you are rate-limiting a
stranger's service to assert a property of your own code. Run it against a
recording and the experiment is deterministic, offline, free, and identical on
every machine -- and the recording itself is reviewable, which a live call is
not.

## Layout

    fixtures/<tool>/<version>/
        docs.md          the documentation snapshot the run was judged against
        traffic.jsonl    redacted requests and responses, one JSON object per line
        expected.yaml    externally verifiable contract facts, or a qualitative marker
        manifest.json    provenance: source, capture time, hashes, redaction, allowed ops

`<version>` is a capture identity, not a semantic version. Two captures of the
same tool at different times are two versions, which is what makes a stale
belief observable: the belief store carries across, the world underneath it
changed, and the demotion is measurable in observations rather than asserted.

## The four files

**`docs.md`** is what the tool promised at capture time. Beliefs are judged
against this snapshot, not against whatever the vendor's page says today --
otherwise a doc edit silently rewrites the answer key for a recording that
never changed.

**`traffic.jsonl`** is the recording. Each line carries `op`, `method`, `path`,
`request`, `status`, `response`, and a `key` that is the SHA-256 of the
operation plus the canonicalised request. Replay is a lookup on that key, so a
probe that asks a question the capture never asked gets a loud miss rather than
a plausible-looking answer. A fixture that silently returns `None` for an
unrecorded call is the same bug as a probe that reads an error body as data.

**`expected.yaml`** is the answer key, in the same shape as
`lab/ground_truth.yaml`: `class`, `operation`, `parameter`, `doc_claims`,
`truth`. Only facts that can be checked by someone else from the vendor's own
documentation and the recorded traffic belong here. A capture with no such
ground truth sets `qualitative: true` and is **excluded from precision and
recall** -- it can demonstrate that the loop runs against a real tool, which is
worth something, but it cannot contribute to a score, because there is nothing
to be right or wrong against.

**`manifest.json`** is the provenance record: source URL, capture timestamp,
the content hash of `docs.md` and `traffic.jsonl`, the redaction result, the
capture driver and version, and the operations the fixture is allowed to serve.
The hashes are what make "this run used this recording" checkable after the
fact rather than assumed.

## Rules

- **Read-only.** No mutating call is ever made against a third-party service.
  The capture driver refuses any method other than GET, and the manifest's
  `allowed_operations` is enforced at replay time as well as at capture time.
- **Credential-free.** The public tools here need no key, so the recording
  carries no secret to leak. The redaction pass runs anyway and records its
  result, because "there was nothing to redact" is a finding that should be
  written down rather than assumed.
- **Qualitative unless there is an answer key.** Precision and recall are
  claims about being right. They require something to be right about.
