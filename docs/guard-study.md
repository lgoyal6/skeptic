# Recorded guard study

`python -m bench.guard_study workflows.json --fixtures RECORDINGS --output NEW_DIR --arm no`
replays fixed recorded workflows through the existing strict `FixtureAdapter`.
The static arm requires `--rules` containing frozen documented `Rule` objects.
The learned arm requires `--beliefs` and compiles only confirmed beliefs with
recorded confirmation and falsification evidence. It refuses rule-file injection.
Each arm writes decisions before any scorer joins the separate answer labels.
Output directories cannot be overwritten. Keep study inputs and outputs outside
the source checkout.

Workflow records contain only ID, goal family, API family/version, split,
tool/capture identity and exact operation/path/request calls. Discovery,
certification and version-transfer inputs have explicit lineage. Versions,
mutants, paraphrases and entity substitutions of one goal are one cluster.
Unique IDs alone cannot establish independence. Admission and permissions
require a separately frozen provenance manifest and independent inspection.

`confirm` writes predictions before two distinct discovery probes. A fixture
miss, failed transport status or empty observation cannot confirm a belief.
Checks use a finite handwritten registry of required fields, types and equality,
not generated executable code. Existing beliefs cannot be overwritten. Learned
checks may carry a finite `request_keys` condition, such as requiring an hourly
field only when the request selects hourly data. An unexercised condition or
empty wildcard observation cannot confirm a belief. Learned
checks apply only to their exact declared API version; transfer and adaptation
need separately recorded evidence. Confirmation on two recordings is bounded
support and does not itself establish a newly discovered API defect.

`score` is a separate scorer-only interface. It verifies matching populations
and versions, retains inconclusive workflow denominators, and reuses the
existing deterministic external-holdout arithmetic. No label or expected.yaml
is read by the runner. Filesystem isolation and frozen input hashes are required
at study launch. The fixed-trace runner measures refusal behavior, not adaptive
agent task completion. No efficacy result follows from controls or code alone.

Retain provider attribution, license notices, request/response provenance,
predictions, failed runs and adverse results. Public benchmark tasks and API
content remain attributable to their original authors.
