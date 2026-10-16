"""Strict model-candidate contracts for the existing two-probe guard study."""
from __future__ import annotations

import json
import re
from bench.guard_study import KINDS, Rule, TYPES, validate_workflows

FIELDS = {"kind", "pointer", "value", "doc_claim", "request_keys", "predictions"}
POINTER = r"^(?:/(?:[^~/]|~[01])*)*$"


def candidate_schema(excerpt: str, quotes: list[str], workflow: dict) -> dict:
    """Constrain generation to supported kinds, literal predictions and real quotes.

    Only discovery documentation and probe requests enter this schema. Responses,
    answer keys and certification records are not arguments to this function.
    """
    validate_workflows([workflow])
    calls = workflow["calls"]
    if workflow["split"] != "discovery" or len(calls) != 2 or calls[0] == calls[1]:
        raise ValueError("candidate generation requires two distinct discovery requests")
    if not isinstance(excerpt, str) or not quotes or any(not isinstance(q, str) or not 1 <= len(q) <= 160 or q not in excerpt for q in quotes):
        raise ValueError("quotes must be exact bounded excerpts")
    if any(not isinstance(k, str) or not k for call in calls for k in call["request"]):
        raise ValueError("request conditions need named keys")
    keys = sorted(set(calls[0]["request"]) & set(calls[1]["request"]))
    properties = {
        "pointer": {"type": "string", "maxLength": 96, "pattern": POINTER},
        "doc_claim": {"type": "string", "enum": quotes},
        "request_keys": {"type": "array", "uniqueItems": True,
                         **({"items": {"type": "string", "enum": keys}} if keys else {"maxItems": 0})},
        "predictions": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "boolean"}},
    }
    values = {"required": {"type": "null"}, "type": {"type": "string", "enum": sorted(TYPES)},
              "equals": {"anyOf": [{"type": "string", "maxLength": 48}, {"type": "number"},
                                   {"type": "boolean"}, {"type": "null"}]}}
    return {"anyOf": [
        {"type": "object", "additionalProperties": False, "required": sorted(FIELDS),
         "properties": {**properties, "kind": {"type": "string", "enum": [kind]}, "value": value}}
        for kind, value in values.items()
    ] + [{"type": "object", "additionalProperties": False, "required": ["abstain"],
          "properties": {"abstain": {"type": "boolean", "enum": [True]}}}]}


def parse_candidate(text: str, *, excerpt: str, quotes: list[str], workflow: dict,
                    rule_id: str) -> tuple[Rule, list[bool]] | None:
    """Decode without repair or coercion; confirmation still belongs to confirm()."""
    candidate_schema(excerpt, quotes, workflow)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate candidate key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-finite JSON constant")

    obj = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    if isinstance(obj, dict) and obj == {"abstain": True} and type(obj["abstain"]) is bool:
        return None
    if not isinstance(obj, dict) or set(obj) != FIELDS:
        raise ValueError("candidate fields must be exact")
    if not isinstance(obj["kind"], str) or obj["kind"] not in KINDS:
        raise ValueError("candidate kind is unsupported")
    if not isinstance(obj["pointer"], str) or len(obj["pointer"]) > 96 or not re.fullmatch(POINTER, obj["pointer"]):
        raise ValueError("candidate needs a bounded JSON pointer")
    if not isinstance(obj["doc_claim"], str) or obj["doc_claim"] not in quotes:
        raise ValueError("candidate must use an exact declared quote")
    predictions = obj.pop("predictions")
    if not isinstance(predictions, list) or len(predictions) != 2 or any(type(p) is not bool for p in predictions):
        raise ValueError("candidate needs two literal boolean predictions")
    keys = obj["request_keys"]
    if not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(keys):
        raise ValueError("invalid candidate request conditions")
    if any(k not in call["request"] for k in keys for call in workflow["calls"]):
        raise ValueError("request condition not exercised by both probes")
    if obj["kind"] == "equals" and (isinstance(obj["value"], (dict, list)) or
                                      isinstance(obj["value"], str) and len(obj["value"]) > 48):
        raise ValueError("equality requires a bounded scalar")
    obj.update(rule_id=rule_id, api_family=workflow["api_family"],
               api_version=workflow["api_version"], op=workflow["calls"][0]["op"])
    if any(call["op"] != obj["op"] for call in workflow["calls"]):
        raise ValueError("probe operations must match")
    return Rule.parse(obj), predictions
