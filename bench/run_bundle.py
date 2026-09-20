"""Build and verify a replayable, structured evidence bundle.

This is the inspectable offline counterpart to a live learning run. It uses a
committed, redacted fixture, applies one probe-selection policy, and records
each decision before reading the selected response. No network, credentials,
or model calls are used, so zeroed model-usage fields are an explicit
measurement boundary rather than missing accounting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

import yaml

from bench.policies import POLICIES, Claim, PolicyView, Probe
from fixtures.checks import CONTRADICTS, SUPPORTS, UNOBSERVABLE, evaluate, load_traffic
from fixtures.format import fixture_dir, read_manifest, verify
from shim.corpus_guards import compile_from_fixture, uncompiled_mismatches

BUNDLE_SCHEMA = "skeptic.run-bundle.v1"
EVENT_SCHEMA = "skeptic.event.v1"
EVENT_TYPES = {
    "anomaly_detected",
    "hypotheses_minted",
    "probe_selected",
    "prediction_committed",
    "observation_rejected_as_uninformative",
    "belief_confirmed",
    "belief_falsified",
    "belief_contested",
    "belief_retired",
    "guard_compiled",
    "guard_refused",
    "corrected_specification_published",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _selection_reason(policy: str) -> str:
    return {
        "fixed": "first remaining probe in capture order",
        "random": "uniform choice from remaining probes using the recorded seed",
        "greedy": "maximized open documented claims touched, then numeric boundary distance",
        "eig": "maximized estimated posterior-entropy reduction per recorded call",
    }[policy]


class EventStream:
    """Append-only, sequence-numbered events for one deterministic run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: list[dict[str, Any]] = []

    def emit(self, event: str, **payload: Any) -> None:
        if event not in EVENT_TYPES:
            raise ValueError(f"unknown event type {event!r}")
        self.events.append({
            "schema": EVENT_SCHEMA,
            "sequence": len(self.events) + 1,
            "run_id": self.run_id,
            "event": event,
            "payload": payload,
        })


def _rules(tool: str, version: str, root: Path) -> list[dict[str, Any]]:
    path = fixture_dir(tool, version, root) / "expected.yaml"
    return list((yaml.safe_load(path.read_text()) or {}).get("rules") or [])


def _belief_before(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rule["id"],
        "operation": str(rule.get("operation") or ""),
        "parameter": rule.get("parameter"),
        "status": "hypothesis",
        "rivals": ["documentation_holds", "documentation_is_contradicted"],
        "verdict": None,
        "evidence": [],
    }


def _final_state(rule: dict[str, Any], verdict: Any) -> dict[str, Any]:
    if verdict.outcome == CONTRADICTS:
        status = "confirmed"
    elif verdict.outcome == SUPPORTS:
        status = "falsified"
    else:
        status = "contested"
    return {
        **_belief_before(rule),
        "status": status,
        "verdict": verdict.outcome,
        "detail": verdict.detail,
        "evidence": list(verdict.evidence),
    }


def build_bundle(
    tool: str,
    version: str,
    *,
    policy: str = "greedy",
    seed: int = 1337,
    root: str | Path = "fixtures",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Replay one fixture under a bounded selector and return its full bundle."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; choose from {sorted(POLICIES)}")

    root = Path(root)
    problems = verify(tool, version, root)
    if problems:
        raise ValueError(f"fixture failed verification: {problems}")

    manifest = read_manifest(tool, version, root)
    rules = _rules(tool, version, root)
    rows = load_traffic(tool, version, root)
    by_key = {r["key"]: r for r in rows}
    run_id = run_id or f"{tool}-{version}-{policy}-seed{seed}"
    events = EventStream(run_id)

    before = [_belief_before(r) for r in rules]
    events.emit(
        "hypotheses_minted",
        count=len(before),
        belief_ids=[b["id"] for b in before],
        rivals=["documentation_holds", "documentation_is_contradicted"],
        source="externally_verifiable_fixture_contracts",
    )

    probes = [Probe(key=r["key"], op=r["op"], request=r["request"]) for r in rows]
    claims = [Claim(rule_id=r["id"], operation=str(r.get("operation") or ""),
                    parameter=r.get("parameter")) for r in rules]
    view = PolicyView(unobserved=list(probes), observed=[], claims=claims)
    rng = random.Random(seed)
    selected: list[str] = []
    probe_log: list[dict[str, Any]] = []
    last_outcome = {r["id"]: UNOBSERVABLE for r in rules}

    full = {r["id"]: evaluate(r["id"], rows).outcome for r in rules}
    settleable = {rid: outcome for rid, outcome in full.items()
                  if outcome != UNOBSERVABLE}

    while view.unobserved:
        probe = POLICIES[policy](view, rng)
        open_before = [c.rule_id for c in view.open_claims]
        events.emit(
            "probe_selected",
            probe_key=probe.key,
            policy=policy,
            why=_selection_reason(policy),
            candidates_remaining=len(view.unobserved),
            open_beliefs=open_before,
            estimated_calls=probe.cost,
        )
        events.emit(
            "prediction_committed",
            probe_key=probe.key,
            predictions=[
                {"belief_id": rid,
                 "documentation_holds": SUPPORTS,
                 "documentation_is_contradicted": CONTRADICTS}
                for rid in open_before
            ],
        )

        view.unobserved.remove(probe)
        view.observed.append(probe)
        selected.append(probe.key)
        row = by_key[probe.key]
        seen = [by_key[k] for k in selected]
        changed: list[dict[str, Any]] = []

        for claim in claims:
            if claim.settled:
                continue
            verdict = evaluate(claim.rule_id, seen)
            if verdict.outcome == UNOBSERVABLE:
                if claim.operation == probe.op and probe.mentions(claim.parameter):
                    events.emit(
                        "observation_rejected_as_uninformative",
                        probe_key=probe.key,
                        belief_id=claim.rule_id,
                        because=verdict.detail,
                        evidence=verdict.evidence,
                    )
                continue

            claim.verdict = verdict.outcome
            claim.settled = True
            last_outcome[claim.rule_id] = verdict.outcome
            changed.append({"belief_id": claim.rule_id, **verdict.to_dict()})
            if verdict.outcome == CONTRADICTS:
                events.emit(
                    "anomaly_detected",
                    belief_id=claim.rule_id,
                    operation=claim.operation,
                    parameter=claim.parameter,
                    detail=verdict.detail,
                    evidence=verdict.evidence,
                )
                events.emit(
                    "belief_confirmed",
                    belief_id=claim.rule_id,
                    probe_key=probe.key,
                    evidence=verdict.evidence,
                )
            else:
                events.emit(
                    "belief_falsified",
                    belief_id=claim.rule_id,
                    probe_key=probe.key,
                    because="recorded behavior supports the documentation",
                    evidence=verdict.evidence,
                )

        probe_log.append({
            "key": probe.key,
            "operation": probe.op,
            "request": probe.request,
            "response": row["response"],
            "status": row["status"],
            "headers": row.get("headers") or {},
            "body_sha256": row.get("body_sha256"),
            "settled": changed,
        })
        if all(last_outcome.get(rid) == outcome for rid, outcome in settleable.items()):
            break

    final_verdicts = {r["id"]: evaluate(r["id"], [by_key[k] for k in selected])
                      for r in rules}
    after = [_final_state(r, final_verdicts[r["id"]]) for r in rules]

    for belief in after:
        if belief["verdict"] == UNOBSERVABLE:
            events.emit(
                "belief_contested",
                belief_id=belief["id"],
                because=belief["detail"],
                evidence=belief["evidence"],
            )

    guards = compile_from_fixture(tool, version, str(root))
    guard_by_rule = {g.rule_id: g for g in guards}
    for belief in after:
        if belief["verdict"] != CONTRADICTS:
            continue
        guard = guard_by_rule.get(belief["id"])
        if guard is None:
            events.emit(
                "guard_refused",
                belief_id=belief["id"],
                because="no executable guard exists for this confirmed mismatch",
            )
        else:
            events.emit(
                "guard_compiled",
                belief_id=belief["id"],
                guard=guard.name,
                provenance=guard.to_dict(),
            )

    corrected = [{
        "belief_id": b["id"],
        "status": b["status"],
        "verdict": b["verdict"],
        "detail": b["detail"],
        "evidence": b["evidence"],
    } for b in after]
    events.emit(
        "corrected_specification_published",
        version=1,
        rules=len(corrected),
        confirmed_mismatches=sum(b["verdict"] == CONTRADICTS for b in after),
        abstentions=sum(b["verdict"] == UNOBSERVABLE for b in after),
    )

    bundle: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "run_id": run_id,
        "mode": "offline_fixture_replay",
        # A wall-clock generation timestamp would make the same replay hash
        # differently on every machine. The immutable capture time is the
        # meaningful timestamp for an offline bundle.
        "created_at": manifest["captured_at"],
        "tool": tool,
        "fixture_version": version,
        "selector": {"policy": policy, "seed": seed, "budget_calls": len(rows)},
        "source": {
            "fixture": f"{tool}/{version}",
            "docs_sha256": manifest["docs_sha256"],
            "fixture_sha256": manifest["traffic_sha256"],
            "manifest_sha256": _sha256_bytes(
                (fixture_dir(tool, version, root) / "manifest.json").read_bytes()),
        },
        "belief_store": {"before": before, "after": after},
        "probe_log": probe_log,
        "model_usage_by_role": {
            "executor": {"calls": 0, "prompt": 0, "completion": 0},
            "reflector": {"calls": 0, "prompt": 0, "completion": 0},
            "boundary": "offline replay uses no model calls",
        },
        "generated_guards": [g.to_dict() for g in guards],
        "guard_refusals": uncompiled_mismatches(tool, version, str(root)),
        "corrected_specification": {"version": 1, "rules": corrected},
        "events": events.events,
    }
    bundle["bundle_sha256"] = _sha256_bytes(_canonical_bytes(bundle))
    return bundle


def verify_bundle(bundle: dict[str, Any], root: str | Path = "fixtures") -> list[str]:
    """Verify a bundle against itself and the committed source fixture."""
    problems: list[str] = []
    if bundle.get("schema") != BUNDLE_SCHEMA:
        problems.append(f"unsupported bundle schema {bundle.get('schema')!r}")

    claimed = str(bundle.get("bundle_sha256") or "")
    body = dict(bundle)
    body.pop("bundle_sha256", None)
    actual = _sha256_bytes(_canonical_bytes(body))
    if claimed != actual:
        problems.append(f"bundle hash mismatch: claimed {claimed}, actual {actual}")

    tool, version = str(bundle.get("tool") or ""), str(bundle.get("fixture_version") or "")
    source = bundle.get("source") or {}
    try:
        manifest = read_manifest(tool, version, root)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"source fixture unavailable: {type(exc).__name__}: {exc}")
        return problems
    if source.get("docs_sha256") != manifest.get("docs_sha256"):
        problems.append("documentation hash no longer matches the source fixture")
    if source.get("fixture_sha256") != manifest.get("traffic_sha256"):
        problems.append("traffic hash no longer matches the source fixture")
    manifest_path = fixture_dir(tool, version, root) / "manifest.json"
    if source.get("manifest_sha256") != _sha256_bytes(manifest_path.read_bytes()):
        problems.append("manifest hash no longer matches the source fixture")

    events = bundle.get("events") or []
    for i, event in enumerate(events, 1):
        if event.get("schema") != EVENT_SCHEMA:
            problems.append(f"event {i} has unsupported schema")
        if event.get("sequence") != i:
            problems.append(f"event {i} has sequence {event.get('sequence')!r}")
        if event.get("event") not in EVENT_TYPES:
            problems.append(f"event {i} has unknown type {event.get('event')!r}")
        if event.get("run_id") != bundle.get("run_id"):
            problems.append(f"event {i} belongs to another run")

    selected = {p.get("key") for p in bundle.get("probe_log") or []}
    fixture_keys = {r["key"] for r in load_traffic(tool, version, root)}
    unknown = selected - fixture_keys
    if unknown:
        problems.append(f"probe log contains keys absent from the fixture: {sorted(unknown)}")
    cited = {
        key
        for belief in (bundle.get("belief_store") or {}).get("after") or []
        for key in belief.get("evidence") or []
    }
    if cited - selected:
        problems.append(
            f"beliefs cite exchanges absent from the probe log: {sorted(cited - selected)}")

    selector = bundle.get("selector") or {}
    policy = selector.get("policy")
    seed = selector.get("seed")
    run_id = bundle.get("run_id")
    if policy not in POLICIES:
        problems.append(f"bundle names unknown selector policy {policy!r}")
    elif not isinstance(seed, int):
        problems.append(f"bundle seed is not an integer: {seed!r}")
    elif not isinstance(run_id, str) or not run_id:
        problems.append("bundle has no run id")
    else:
        # A content hash only detects accidental edits when an attacker does not
        # recompute it. Rebuild the deterministic run from the committed fixture
        # as well, so a re-hashed probe, verdict, event, or guard still fails.
        expected = build_bundle(
            tool,
            version,
            policy=policy,
            seed=seed,
            root=root,
            run_id=run_id,
        )
        if expected != bundle:
            problems.append("bundle does not reproduce from the committed source fixture")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", default="frankfurter")
    ap.add_argument("--version", default="v2-2026-09-19")
    ap.add_argument("--policy", choices=sorted(POLICIES), default="greedy")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--root", default="fixtures")
    ap.add_argument("--out", default="")
    ap.add_argument("--verify", default="")
    args = ap.parse_args()

    if args.verify:
        path = Path(args.verify)
        bundle = json.loads(path.read_text())
        problems = verify_bundle(bundle, args.root)
        if problems:
            for problem in problems:
                print(f"  ! {problem}")
            return 1
        print(f"  verified {path}: {bundle['bundle_sha256']}")
        return 0

    bundle = build_bundle(args.tool, args.version, policy=args.policy,
                          seed=args.seed, root=args.root)
    out = Path(args.out) if args.out else (
        Path("runs/bundles") / f"{bundle['run_id']}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2) + "\n")
    print(f"  wrote {out}")
    print(f"  {len(bundle['probe_log'])} probes, {len(bundle['events'])} events, "
          f"{len(bundle['generated_guards'])} guards")
    print(f"  sha256 {bundle['bundle_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
