"""Replay recorded API calls for finite guard studies.
Reuse existing fixtures, belief records, and compiled checks.
Score separately after replay decisions are sealed."""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from agent.adapters.fixture import FixtureAdapter, FixtureMiss
from agent.beliefs import Belief, BeliefStore, Status
from bench.external_holdout import evaluate
from shim.corpus_guards import CompiledGuard


WORKFLOW_KEYS = {"workflow_id", "goal_family", "api_family", "api_version", "split",
                 "tool", "capture_id", "calls"}
CALL_KEYS = {"op", "path", "request"}
KINDS = {"required", "type", "equals"}
TYPES = {"object": dict, "array": list, "string": str, "number": (int, float),
         "boolean": bool, "null": type(None)}
MISSING = object()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def append(path: Path, value: dict) -> None:
    with path.open("a") as stream:
        stream.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        stream.flush()


def validate_workflows(rows: list[dict]) -> None:
    """Require lineage and exact public fields, rejecting embedded answer keys."""
    ids, families = set(), {}
    for row in rows:
        if set(row) != WORKFLOW_KEYS:
            raise ValueError("workflow fields must be label-free and exact")
        for key in WORKFLOW_KEYS - {"calls"}:
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"missing workflow {key}")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", row[k])
               for k in ("workflow_id", "tool", "capture_id")):
            raise ValueError("recording and workflow identities must be safe path components")
        if row["split"] not in {"discovery", "certification", "version_transfer"}:
            raise ValueError("unknown split")
        if row["workflow_id"] in ids:
            raise ValueError("duplicate workflow_id")
        ids.add(row["workflow_id"])
        cluster = (row["api_family"], row["goal_family"])
        previous = families.setdefault(cluster, set())
        previous.add(row["split"])
        # Version siblings may transfer, but certification cannot borrow a
        # discovery goal and call it an independent honest workflow.
        if {"discovery", "certification"} <= previous:
            raise ValueError("goal lineage crosses discovery/certification")
        if not isinstance(row["calls"], list) or not row["calls"]:
            raise ValueError("workflow needs calls")
        for call in row["calls"]:
            if set(call) != CALL_KEYS or not isinstance(call["request"], dict):
                raise ValueError("call fields must be exact")
            if not all(isinstance(call[k], str) and call[k] for k in ("op", "path")):
                raise ValueError("call needs operation and path")


def values_at(value: Any, pointer: str) -> list[Any]:
    """JSON pointer plus array wildcard; no expression evaluation."""
    values = [value]
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        found = []
        for item in values:
            if part == "*" and isinstance(item, list):
                found.extend(item)
            elif isinstance(item, dict):
                found.append(item.get(part, MISSING))
            elif isinstance(item, list) and part.isdigit() and int(part) < len(item):
                found.append(item[int(part)])
            else:
                found.append(MISSING)
        values = found
    return values


@dataclass(frozen=True)
class Rule:
    rule_id: str
    api_family: str
    api_version: str
    op: str
    kind: str
    pointer: str
    value: Any
    doc_claim: str
    request_keys: tuple[str, ...] = ()

    @classmethod
    def parse(cls, data: dict) -> "Rule":
        fields = set(cls.__dataclass_fields__)
        if not fields - {"request_keys"} <= set(data) <= fields:
            raise ValueError("rule fields must be exact")
        data = dict(data)
        keys = data.get("request_keys", ())
        if not isinstance(keys, (list, tuple)) or any(not isinstance(k, str) or not k for k in keys):
            raise ValueError("request_keys must name request parameters")
        data["request_keys"] = tuple(keys)
        rule = cls(**data)
        if rule.kind not in KINDS or not isinstance(rule.pointer, str):
            raise ValueError("unknown check kind or invalid pointer")
        if rule.pointer and not rule.pointer.startswith("/"):
            raise ValueError("pointer must be empty or start with slash")
        if not all(isinstance(getattr(rule, k), str) and getattr(rule, k).strip()
                   for k in ("rule_id", "api_family", "api_version", "op", "doc_claim")):
            raise ValueError("rule needs scope and documented claim")
        if rule.kind == "type" and (not isinstance(rule.value, str) or rule.value not in TYPES):
            raise ValueError("unknown response type")
        if rule.kind == "required" and rule.value is not None:
            raise ValueError("required check has no constant")
        digest(data)
        return rule

    def check(self, exchange: dict) -> str | None:
        if any(k not in (exchange.get("request") or {}) for k in self.request_keys):
            return None
        vals = values_at(exchange["response"], self.pointer)
        def valid(v):
            if v is MISSING:
                return False
            if self.kind == "required":
                return True
            if self.kind == "equals":
                return type(v) is type(self.value) and v == self.value
            return isinstance(v, TYPES[self.value]) and not (self.value == "number" and isinstance(v, bool))
        return None if all(valid(v) for v in vals) else f"{self.rule_id}: {self.kind} failed at {self.pointer}"

    def compiled(self, evidence: list[str], docs_hash: str, traffic_hash: str) -> CompiledGuard:
        return CompiledGuard(self.rule_id, "response_contract", self.api_family, self.api_version,
                             self.rule_id, self.doc_claim, self.check, evidence=evidence,
                             docs_sha256=docs_hash, traffic_sha256=traffic_hash)


def confirm(rule: Rule, workflow: dict, predictions: list[bool], adapter: FixtureAdapter,
            store: BeliefStore, ledger: Path, budget: int) -> Belief:
    """Certify a candidate only from two distinct observable discovery probes.

    These finite contract checks reuse handwritten predicates. Confirmation is
    bounded recording evidence, not proof of universality or a discovered defect.
    """
    validate_workflows([workflow])
    if workflow["split"] != "discovery" or budget < 2:
        raise ValueError("confirmation needs discovery data and two-call budget")
    calls = workflow["calls"]
    if len(calls) != 2 or len({digest(c) for c in calls}) != 2:
        raise ValueError("need distinct confirmation and falsification probes")
    if len(predictions) != 2 or any(type(v) is not bool for v in predictions):
        raise ValueError("commit two boolean predictions")
    if (rule.api_family, rule.api_version) != (workflow["api_family"], workflow["api_version"]):
        raise ValueError("candidate scope differs from discovery")
    if rule.doc_claim not in adapter.docs:
        raise ValueError("documented claim must quote discovery documentation")
    if rule.rule_id in store.beliefs:
        raise ValueError("existing beliefs require a new versioned ID, not overwrite")
    belief = Belief(rule.rule_id, store.tool, rule.op, "response_contract", rule.doc_claim,
                    json.dumps(rule.__dict__, sort_keys=True))
    store.beliefs[belief.id] = belief
    evidence = []
    for index, (call, predicted) in enumerate(zip(calls, predictions)):
        if call["op"] != rule.op:
            raise ValueError("probe operation differs from candidate")
        if any(k not in call["request"] for k in rule.request_keys):
            append(ledger, {"event": "inconclusive", "index": index,
                            "reason": "probe does not exercise request condition"})
            break
        append(ledger, {"event": "prediction", "index": index, "rule": rule.__dict__,
                        "call": call, "predicts_pass": predicted})
        try:
            status, body = adapter.call(call["op"], _path=call["path"], **call["request"])
        except (FixtureMiss, TimeoutError, OSError) as exc:
            append(ledger, {"event": "inconclusive", "index": index, "reason": str(exc)})
            break
        if status != 200 or body is None or body in ({}, []):
            append(ledger, {"event": "inconclusive", "index": index, "status": status})
            break
        if not values_at(body, rule.pointer):
            append(ledger, {"event": "inconclusive", "index": index,
                            "reason": "wildcard probe observed no elements"})
            break
        passed = rule.check({"response": body, "request": call["request"]}) is None
        append(ledger, {"event": "observation", "index": index, "passed": passed,
                        "response_sha256": digest(body)})
        if passed != predicted or not passed:
            belief.falsify(f"probe-{index}", "candidate contradicted or prediction failed")
            break
        evidence.append(digest(call))
    if len(evidence) == 2:
        belief.survived_falsification = evidence[1]
        belief.confirm(evidence[0], workflow["workflow_id"])
        belief.note("recorded_evidence", json.dumps(evidence))
    store.save()
    return belief


def learned_rules(store: BeliefStore) -> list[Rule]:
    out = []
    for belief in store.active():
        if belief.cls != "response_contract" or not belief.survived_falsification or not belief.probes:
            continue
        if not any(h["event"] == "recorded_evidence" for h in belief.history):
            continue
        out.append(Rule.parse(json.loads(belief.belief)))
    return out


def run_trace(workflow: dict, arm: str, adapter: FixtureAdapter, rules: list[Rule]) -> dict:
    validate_workflows([workflow])
    if arm not in {"no", "static", "learned"}:
        raise ValueError("unknown arm")
    relevant = [] if arm == "no" else [r for r in rules if
        (r.api_family, r.api_version) == (workflow["api_family"], workflow["api_version"])]
    events = []
    for call in workflow["calls"]:
        try:
            status, body = adapter.call(call["op"], _path=call["path"], **copy.deepcopy(call["request"]))
        except (FixtureMiss, TimeoutError, OSError) as exc:
            events.append({"status": "inconclusive", "reason": str(exc)})
            continue
        if status != 200:
            events.append({"status": "inconclusive", "http_status": status})
            continue
        exchange = {**call, "response": copy.deepcopy(body), "status": status,
                    "headers": dict(adapter.last_headers)}
        refusals = [reason for rule in relevant if rule.op == call["op"]
                    for reason in [rule.compiled([], adapter.manifest["docs_sha256"],
                       adapter.manifest["traffic_sha256"])(exchange)] if reason]
        events.append({"status": "deny" if refusals else "allow", "refusals": refusals,
                       "response_sha256": digest(body)})
    observed = "inconclusive" if any(e["status"] == "inconclusive" for e in events) else (
        "deny" if any(e["status"] == "deny" for e in events) else "allow")
    return {"workflow_id": workflow["workflow_id"], "arm": arm, "observed": observed,
            "events": events, "calls": len(events), "goal_family": workflow["goal_family"],
            "api_family": workflow["api_family"], "api_version": workflow["api_version"]}


def score(observations: list[dict], labels: list[dict]) -> dict:
    """Scorer-only join, after predictions/decisions have been sealed elsewhere."""
    indexed = {r["workflow_id"]: r for r in observations}
    if len(indexed) != len(observations) or len({r["arm"] for r in observations}) != 1:
        raise ValueError("score one arm with unique workflow IDs")
    if len({r["workflow_id"] for r in labels}) != len(labels) or set(indexed) != {r["workflow_id"] for r in labels}:
        raise ValueError("label/observation populations differ")
    binary, inconclusive = [], []
    for label in labels:
        if set(label) != {"workflow_id", "expected", "tool_version", "provenance"} or label["expected"] not in {"allow", "deny"}:
            raise ValueError("invalid scorer label")
        obs = indexed[label["workflow_id"]]
        if label["tool_version"] != obs["api_version"]:
            raise ValueError("label version differs from executed version")
        if obs["observed"] == "inconclusive":
            inconclusive.append(label["workflow_id"])
        else:
            binary.append({**label, "split": "holdout", "observed": obs["observed"]})
    return {"attempted_n": len(labels), "inconclusive": inconclusive,
            "attempted_valid_n": sum(r["expected"] == "allow" for r in labels),
            "attempted_invalid_n": sum(r["expected"] == "deny" for r in labels),
            "inconclusive_valid_n": sum(r["expected"] == "allow" and r["workflow_id"] in inconclusive for r in labels),
            "inconclusive_invalid_n": sum(r["expected"] == "deny" and r["workflow_id"] in inconclusive for r in labels),
            "binary_summary": evaluate(binary) if binary else None,
            "scope": "Fixed recorded traces; no adaptive agent task-completion claim."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflows", type=Path)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=["no", "static", "learned"], required=True)
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--beliefs", type=Path)
    args = parser.parse_args()
    rows = json.loads(args.workflows.read_text()); validate_workflows(rows)
    if args.output.exists():
        raise ValueError("refuse to overwrite decisions")
    if args.arm == "learned":
        if args.beliefs is None or args.rules is not None:
            raise ValueError("learned arm requires belief store, not rule-file injection")
        rules = learned_rules(BeliefStore("guard-study", args.beliefs))
    else:
        if args.arm == "static" and args.rules is None:
            raise ValueError("static arm requires frozen documented rules")
        if args.arm == "no" and (args.rules is not None or args.beliefs is not None):
            raise ValueError("no-guard arm cannot receive guard inputs")
        rules = [Rule.parse(r) for r in json.loads(args.rules.read_text())] if args.rules else []
    args.output.mkdir(parents=True)
    for workflow in rows:
        adapter = FixtureAdapter(tool=workflow["tool"], version=workflow["capture_id"],
            run_id=workflow["workflow_id"], fixtures_root=str(args.fixtures), runs_dir=str(args.output))
        try:
            append(args.output / "decisions.jsonl", run_trace(workflow, args.arm, adapter, rules))
        finally:
            adapter.close()


if __name__ == "__main__":
    main()
