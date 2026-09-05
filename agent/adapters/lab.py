"""
Adapter for the lab tool.

Two jobs beyond making HTTP calls:

1. Log every request and response to runs/<run_id>.jsonl. That log is what
   counterfactual replay reads later, so it has to be complete and ordered.

2. Feed each call to the ContractLayer and surface anomalies.

The adapter also applies any guards handed to it. That is the mechanism the
exported shim reuses: same guard objects, same enforcement, so what the agent
learns and what the shim enforces cannot drift apart.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from agent.contract import Anomaly, Call, ContractLayer

BASE = "http://127.0.0.1:8077"


class LabAdapter:
    name = "lab"

    def __init__(
        self,
        run_id: str,
        base: str = BASE,
        guards: list[Any] | None = None,
        runs_dir: str | Path = "runs",
    ) -> None:
        self.run_id = run_id
        self.client = httpx.Client(base_url=base, timeout=30.0)
        self.contract = ContractLayer()
        self.guards = guards or []
        self.n = 0
        self.anomalies: list[Anomaly] = []
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.runs_dir / f"{run_id}.jsonl"
        self.tokens_spent = 0
        self.t0 = time.time()

    # --- plumbing ---------------------------------------------------------

    def _write(self, rec: dict[str, Any]) -> None:
        with self.log_path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _call(self, op: str, method: str, path: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        # pre-call guards may rewrite the request or refuse it outright
        refusal = None
        for g in self.guards:
            payload, refusal = g.before(op, payload)
            if refusal:
                break
        if refusal:
            self.n += 1
            rec = {
                "n": self.n, "t": time.time(), "op": op, "request": payload,
                "status": 0, "response": {"refused_by_guard": refusal}, "guard": refusal,
            }
            self._write(rec)
            return 0, {"refused_by_guard": refusal}

        self.n += 1
        t = time.time()
        if method == "GET":
            r = self.client.get(path)
        elif method == "PATCH":
            r = self.client.patch(path, json=payload or {})
        else:
            r = self.client.post(path, json=payload or {})

        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text[:400]}

        if r.status_code == 429 and isinstance(body, dict):
            body = dict(body)
            body["_retry_after_present"] = "Retry-After" in r.headers

        rec = {
            "n": self.n, "t": t, "op": op, "request": payload,
            "status": r.status_code, "response": body,
            "elapsed_ms": round((time.time() - t) * 1000, 1),
        }
        self._write(rec)

        found = self.contract.record(Call(n=self.n, op=op, request=payload or {}, status=r.status_code, response=body, t=t))
        self.anomalies.extend(found)

        # post-call guards may repair the response or trigger a follow-up
        for g in self.guards:
            body = g.after(op, payload, r.status_code, body, self)

        return r.status_code, body

    # --- documented surface ----------------------------------------------

    def create(self, **fields: Any) -> tuple[int, Any]:
        return self._call("create", "POST", "/v1/items", fields)

    def get(self, item_id: str) -> tuple[int, Any]:
        return self._call("get", "GET", f"/v1/items/{item_id}", {"id": item_id})

    def update(self, item_id: str, **fields: Any) -> tuple[int, Any]:
        return self._call("update", "PATCH", f"/v1/items/{item_id}", fields)

    def archive(self, item_id: str) -> tuple[int, Any]:
        return self._call("archive", "POST", f"/v1/items/{item_id}/archive", {"id": item_id})

    def bulk_create(self, items: list[dict[str, Any]]) -> tuple[int, Any]:
        return self._call("bulk_create", "POST", "/v1/bulk_create", {"items": items})

    def search(self, **params: Any) -> tuple[int, Any]:
        return self._call("search", "POST", "/v1/search", params)

    # --- introspection ----------------------------------------------------

    def anomaly_signatures(self) -> list[str]:
        return [a.signature() for a in self.anomalies]

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "calls": self.n,
            "anomalies": len(self.anomalies),
            "signatures": sorted(set(self.anomaly_signatures())),
            "wall_s": round(time.time() - self.t0, 2),
        }

    def close(self) -> None:
        self.client.close()
