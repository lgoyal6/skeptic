"""
Designing and running the experiment.

Given rival hypotheses, pick the template whose outcome differs most between
them per call spent, have the reflector fill in its parameters and state what
each hypothesis PREDICTS, run it, then compare prediction to observation.

Predicting before observing is the part that matters. A model shown a result
and asked "which hypothesis does this support" will rationalise almost
anything. A model that had to commit to predictions first can be caught out,
and a hypothesis whose prediction did not come true is falsified whatever the
model would prefer to say afterwards.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from agent.adapters.lab import LabAdapter
from agent.beliefs import Belief, BeliefStore, Status
from agent.llm import LLM
from reflect.hypothesis import Hypothesis
from reflect.templates import TEMPLATES, Observation, catalogue, choose

# A reasoning model starves its own answer at a low cap: it spends the whole
# budget thinking and returns content=null with finish_reason=length, then
# json_chat doubles and retries. Measured on the real design prompt:
#   6000 tokens -> 61s, starved, no content
#  14000 tokens -> 34s, succeeded first time
# The ladder cost 6x. Start where it actually fits.
DESIGN_BUDGET = 14000
VERDICT_BUDGET = 16000

DESIGN_PROMPT = """You are designing ONE experiment to tell rival explanations apart.

RIVAL HYPOTHESES
{hyps}

AVAILABLE EXPERIMENT TEMPLATES
{catalogue}

RANKED BY DISCRIMINATION PER CALL (highest first)
{ranked}

Pick ONE template and give its parameters. Then, BEFORE seeing any result,
state what each hypothesis predicts the outcome will be. Predictions must
differ between hypotheses -- if they are all the same, the experiment is
useless, so pick a different template.

Template parameter shapes:
  boundary       {{"op":"search"|"create","param":"<name>","values":[...],"base":{{...}}}}
  timing         {{"vendor":"<v>","wait_s":3.0,"base":{{"filter":{{"vendor":"<v>"}},"page_size":50}}}}
  idempotency    {{"vendor":"<v>","attempts":40}}
  consistency    {{"fields":{{"<field>":<value>}},"vendor":"<v>"}}
  ordering       {{"vendor":"<v>","sort":"created"}}
  header_burst   {{"n":10}}
  flag_discovery {{"flag":"<name>","value":true,"base":{{"filter":{{"vendor":"<v>"}},"page_size":50}}}}

Use vendor "{vendor}" for anything you create, so this experiment cannot
disturb other data.

Reply with a single JSON object:
{{
  "template": "<name>",
  "params": {{...}},
  "why_this_one": "<one sentence>",
  "predictions": [
    {{"hypothesis_id": "<id from the list>", "predicts": "<what this hypothesis says the facts will show>"}}
  ]
}}"""

VERDICT_PROMPT = """An experiment was run to separate rival hypotheses.

WHAT EACH HYPOTHESIS PREDICTED (committed before the experiment ran)
{predictions}

WHAT ACTUALLY HAPPENED
  template: {template}
  params  : {params}
  facts   : {facts}
  summary : {narrative}

For each hypothesis, decide whether the observation matches its prediction.

Be strict. "Consistent with" is not "predicted". If a hypothesis predicted a
change and nothing changed, it is falsified. If the experiment turned out not
to separate them, say so with verdict "inconclusive" for all of them rather
than picking a favourite.

A hypothesis can predict correctly while still being worded badly: the
prediction was right but the stated reason was invented. So if anything was
confirmed, restate it as what the EVIDENCE supports, with no causal story you
did not test. "Capped at 50" is supported. "Capped at 50 because of your
subscription tier" is not, unless you tested tiers.

Class must be one of: silent_truncation, silent_coercion,
eventual_consistency, expiry, undocumented_flag, mislabelled_semantics,
idempotency_hazard, rate_limit.

Reply with a single JSON object:
{{
  "verdicts": [
    {{"hypothesis_id": "<id>", "verdict": "confirmed"|"falsified"|"inconclusive",
      "because": "<one sentence citing a specific fact>"}}
  ],
  "learned": "<one precise sentence stating only what the evidence supports, or empty>",
  "learned_class": "<class of the learned behaviour, or empty>",
  "learned_parameter": "<parameter involved, or empty>",
  "learned_action": "<what an agent should do about it in future runs, or empty>"
}}"""


@dataclass
class ProbeRecord:
    id: str
    belief_ids: list[str]
    template: str
    params: dict[str, Any]
    why: str
    predictions: list[dict[str, str]]
    observation: dict[str, Any]
    verdicts: list[dict[str, str]]
    learned: str
    calls: int
    wall_s: float
    adversarial: bool = False
    # The restated-from-evidence fields, persisted so a saved probe record is
    # enough to re-apply its verdicts. Without these, a crashed settle run
    # loses every experiment it already paid for.
    learned_class: str = ""
    learned_parameter: str = ""
    learned_action: str = ""

    def save(self, root: str | Path = "probes") -> Path:
        p = Path(root) / f"{self.id}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2))
        return p


def _facts_uninformative(f: dict[str, Any]) -> bool:
    """Did these facts carry any signal at all?"""
    if f.get("error"):
        return True
    sweep = f.get("sweep")
    if isinstance(sweep, list) and sweep:
        seen = [r.get("returned") for r in sweep if "returned" in r]
        if seen and all((v or 0) == 0 for v in seen):
            return True
    if f.get("population") == 0:
        return True
    return False


def _uninformative(obs: Observation) -> bool:
    """Did the experiment actually observe anything?"""
    return _facts_uninformative(obs.facts or {})


def _normalise(s: str) -> str:
    return "".join(c for c in str(s).lower() if c.isalnum() or c == " ").strip()


def indistinguishable(predictions: list[dict[str, str]]) -> list[str]:
    """Hypotheses whose predictions are identical -- the experiment cannot split them."""
    seen: dict[str, list[str]] = {}
    for p in predictions:
        seen.setdefault(_normalise(p.get("predicts", "")), []).append(
            str(p.get("hypothesis_id", ""))
        )
    return [ids[0] for ids in seen.values() if len(ids) > 1]


def design(
    reflector: LLM,
    beliefs: list[Belief],
    vendor: str,
    budget_calls: int = 5,
    exclude: set[str] | None = None,
) -> dict[str, Any]:
    """Choose an experiment, and reject one that cannot separate the rivals.

    The prompt already says a template predicting the same outcome for two
    hypotheses is useless. The model does not always obey: on the pagination
    rivals it picked `timing`, which lumps 'hard cap' and 'vendor-specific
    cap' into one bucket, and duly returned two INCONCLUSIVE verdicts after
    spending the call budget. So the rule is enforced in code, not requested
    in prose.
    """
    exclude = exclude or set()
    classes = [b.cls for b in beliefs]
    ranked = [r for r in choose(classes) if r[0] not in exclude]
    ranked_txt = "\n".join(
        f"  {name}: {score:.2f} rival classes separated per call" for name, score in ranked
    ) or "  (no template matches these classes)"

    hyps_txt = "\n".join(
        f"  id={b.id}\n    class={b.cls} parameter={b.parameter}\n    claim: {b.belief}"
        for b in beliefs
    )

    # If an adversary already said how it would break one of these, use its
    # experiment rather than inventing a weaker one. The adversary reviewed
    # the evidence and named the specific gap; a designer that ignores that
    # is choosing to repeat the mistake it was told about.
    attacks = []
    for b in beliefs:
        for h in reversed(b.history):
            if h.get("event") == "suggested_attack" and h.get("detail"):
                attacks.append(f"  for {b.id}:\n    {h['detail']}")
                break
    attack_txt = ""
    if attacks:
        attack_txt = (
            "\n\nAN ADVERSARY REVIEWED THESE BELIEFS AND SAID THE EVIDENCE WAS\n"
            "INSUFFICIENT. It proposed these experiments as the ones most likely to\n"
            "settle the question properly. Prefer them unless a template genuinely\n"
            "cannot express the experiment:\n" + "\n".join(attacks)
        )

    extra = attack_txt
    if exclude:
        extra = (
            f"\n\nDo NOT use these templates, they already failed to separate these "
            f"hypotheses: {', '.join(sorted(exclude))}."
        )

    data = reflector.json_chat(
        [
            {"role": "system", "content": "You design minimal, decisive experiments. You commit to predictions before seeing results."},
            {"role": "user", "content": DESIGN_PROMPT.format(
                hyps=hyps_txt, catalogue=catalogue(), ranked=ranked_txt, vendor=vendor
            ) + extra},
        ],
        max_tokens=DESIGN_BUDGET,
    )

    name = str(data.get("template", "")).strip()
    if name not in TEMPLATES or name in exclude:
        name = ranked[0][0] if ranked else "boundary"
        data["template"] = name
        data.setdefault("why_this_one", "fell back to the highest-ranked usable template")

    # enforce the rule the prompt states
    if len(beliefs) > 1 and not exclude:
        dupes = indistinguishable(data.get("predictions", []))
        if dupes:
            return design(reflector, beliefs, vendor, budget_calls,
                          exclude={name} | exclude)
    return data


def run_probe(
    reflector: LLM,
    store: BeliefStore,
    beliefs: list[Belief],
    probe_id: str,
    vendor: str,
    runs_dir: str = "runs",
    probes_dir: str = "probes",
    budget_calls: int = 5,
    adversarial: bool = False,
    apply: bool = True,
) -> ProbeRecord:
    """Design, run and judge one experiment.

    `apply=False` returns the record without touching the belief store, so
    several probes can run concurrently (they are almost entirely waiting on
    the model) and have their verdicts applied serially afterwards. The store
    is a single YAML file; concurrent writers would race.
    """
    t0 = time.time()
    plan = design(reflector, beliefs, vendor=vendor, budget_calls=budget_calls)
    tmpl = TEMPLATES[plan["template"]]

    # header_burst measures throttling, so it must be allowed to burst.
    # Everything else paces itself so the rate limiter does not contaminate
    # the variable under test.
    pace = 0.0 if tmpl.name == "header_burst" else 0.4
    adapter = LabAdapter(run_id=probe_id, runs_dir=runs_dir, min_interval=pace)
    try:
        obs: Observation = tmpl.run(adapter, plan.get("params") or {})
    except Exception as e:  # a probe that blows up is a failed probe, not a crash
        obs = Observation(
            template=tmpl.name,
            params=plan.get("params") or {},
            calls=adapter.n,
            facts={"error": f"{type(e).__name__}: {e}"},
            narrative=["probe raised an exception"],
        )
    finally:
        adapter.close()

    if _uninformative(obs):
        # Absence of evidence is not evidence of absence. A sweep that saw
        # nothing cannot refute anything, and letting the model judge it
        # anyway is how a true belief gets destroyed by an empty experiment.
        rec = ProbeRecord(
            id=probe_id, belief_ids=[b.id for b in beliefs], template=obs.template,
            params=obs.params, why=str(plan.get("why_this_one", "")),
            predictions=plan.get("predictions", []), observation=obs.to_dict(),
            verdicts=[{"hypothesis_id": b.id, "verdict": "inconclusive",
                       "because": "the experiment observed no signal; nothing to conclude"}
                      for b in beliefs],
            learned="", calls=obs.calls, wall_s=round(time.time() - t0, 2),
            adversarial=adversarial,
        )
        rec.save(probes_dir)
        rec._learned_payload = {}  # type: ignore[attr-defined]
        if apply:
            _apply(store, rec, adversarial=adversarial, learned={})
        return rec

    verdict_data = reflector.json_chat(
        [
            {"role": "system", "content": "You judge experiments strictly. Consistency is not confirmation."},
            {"role": "user", "content": VERDICT_PROMPT.format(
                predictions=json.dumps(plan.get("predictions", []), indent=2),
                template=obs.template,
                params=json.dumps(obs.params)[:400],
                facts=json.dumps(obs.facts)[:1400],
                narrative="; ".join(obs.narrative)[:400],
            )},
        ],
        max_tokens=VERDICT_BUDGET,
    )

    rec = ProbeRecord(
        id=probe_id,
        belief_ids=[b.id for b in beliefs],
        template=obs.template,
        params=obs.params,
        why=str(plan.get("why_this_one", "")),
        predictions=plan.get("predictions", []),
        observation=obs.to_dict(),
        verdicts=verdict_data.get("verdicts", []),
        learned=str(verdict_data.get("learned", "")),
        learned_class=str(verdict_data.get("learned_class", "")),
        learned_parameter=str(verdict_data.get("learned_parameter", "")),
        learned_action=str(verdict_data.get("learned_action", "")),
        calls=obs.calls,
        wall_s=round(time.time() - t0, 2),
        adversarial=adversarial,
    )
    rec.save(probes_dir)
    rec._learned_payload = verdict_data  # type: ignore[attr-defined]
    if apply:
        _apply(store, rec, adversarial=adversarial, learned=verdict_data)
    return rec


CLASSES = {
    "silent_truncation", "silent_coercion", "eventual_consistency", "expiry",
    "undocumented_flag", "mislabelled_semantics", "idempotency_hazard", "rate_limit",
}


def _rewrite_from_evidence(b: Belief, learned: dict[str, Any]) -> None:
    """Replace a guess with what the experiment actually showed.

    A hypothesis can predict correctly while being worded wrongly -- the
    prediction holds but the stated cause was invented. Confirming the guess
    verbatim would record a falsehood that happened to make a right
    prediction, so the confirmed belief is restated from the evidence.
    """
    text = str(learned.get("learned", "")).strip()
    if not text:
        return
    if text != b.belief:
        b.note("restated_from_evidence", f"was: {b.belief[:120]}")
        b.belief = text

    cls = str(learned.get("learned_class", "")).strip()
    if cls in CLASSES and cls != b.cls:
        # The same wire-evidence constraint that governs minting has to govern
        # restatement, or the verdict step becomes a way around it. It was: a
        # `silent_null` anomaly on due_date came back restated as
        # `silent_truncation`, which no evidence supported, and it scored as a
        # false belief.
        from reflect.hypothesis import constrain_class

        sig = next((h.get("detail") for h in b.history
                    if h.get("event") == "signature"), "")
        kind = sig.split(".", 2)[2] if sig.count(".") >= 2 else ""
        allowed = constrain_class(kind, cls) if kind else cls
        if allowed != cls:
            b.note("class_restatement_rejected",
                   f"verdict proposed {cls}; wire evidence ({kind}) admits only {allowed}")
            cls = allowed
        if cls != b.cls:
            b.note("class_corrected", f"{b.cls} -> {cls}")
            b.cls = cls

    param = str(learned.get("learned_parameter", "")).strip()
    if param and param.lower() not in ("null", "none") and param != b.parameter:
        b.parameter = param

    action = str(learned.get("learned_action", "")).strip()
    if action:
        b.action = action


def _apply(
    store: BeliefStore,
    rec: ProbeRecord,
    adversarial: bool = False,
    learned: dict[str, Any] | None = None,
) -> None:
    """Move beliefs according to the verdicts.

    The uninformative-observation check used to live only in `run_probe`, so
    every other caller bypassed it -- and `bench/apply_probes.py` replays
    saved records from disk straight into here, which is the path used after
    every settle. A record with an empty sweep could still falsify a true
    belief on re-application. The guard belongs at the point of effect, not
    at one of the call sites.
    """
    learned = learned or {}
    obs_facts = (rec.observation or {}).get("facts") or {}
    if _facts_uninformative(obs_facts):
        for v in rec.verdicts:
            b = store.get(str(v.get("hypothesis_id", "")))
            if b is not None:
                b.note("probe_uninformative", f"{rec.id} observed no signal; verdict ignored")
        store.save()
        return

    for v in rec.verdicts:
        b = store.get(str(v.get("hypothesis_id", "")))
        if b is None:
            continue
        verdict = str(v.get("verdict", "inconclusive")).lower()
        because = str(v.get("because", ""))[:200]

        if verdict == "confirmed":
            if adversarial:
                # surviving an attempt to break it is not a fresh confirmation,
                # it is the absence of a refutation
                b.survived_falsification = rec.id
                b.note("survived_falsification", because)
            else:
                b.confirm(rec.id)
                b.posterior.contradicts_doc(2.0)
                b.note("probe_confirmed", because)
                _rewrite_from_evidence(b, learned)
        elif verdict == "falsified":
            b.falsify(rec.id, because)
            b.posterior.supports_doc(1.0)
        else:
            b.note("probe_inconclusive", because)

    # If one experiment confirms two rivals, it did not actually separate
    # them: they are one belief wearing two hats. Keep the best-supported and
    # retire the rest as duplicates, rather than letting memory carry two
    # sentences that say the same thing and halve precision.
    confirmed_now = [
        store.get(str(v.get("hypothesis_id", "")))
        for v in rec.verdicts
        if str(v.get("verdict", "")).lower() == "confirmed"
    ]
    confirmed_now = [b for b in confirmed_now if b is not None]
    if len(confirmed_now) > 1:
        keeper = max(confirmed_now, key=lambda b: (b.posterior.p_doc_correct * -1, len(b.belief)))
        for b in confirmed_now:
            if b.id == keeper.id:
                continue
            b.retire(f"duplicate of {keeper.id}; {rec.id} could not separate them")
            keeper.note("absorbed_duplicate", b.id)

    # a confirmed belief refutes its rivals
    for v in rec.verdicts:
        if str(v.get("verdict", "")).lower() != "confirmed":
            continue
        winner = store.get(str(v.get("hypothesis_id", "")))
        if winner is None:
            continue
        for rival_id in winner.competing:
            r = store.get(rival_id)
            if r and r.status is Status.HYPOTHESIS:
                r.falsify(rec.id, f"rival {winner.id} confirmed by the same experiment")
    store.save()
