"""
Adapter that replays a committed recording instead of calling a live service.

Same contract as `adapters/lab.py` and `adapters/notion.py`: same constructor
shape, the same per-call JSONL record keys, the same `ContractLayer` wiring,
the same guard `before`/`after` hooks, the same `.n` / `.anomalies` /
`.close()` surface. Anything that can drive the lab can drive a fixture
without knowing the difference -- which is the point, because the detection
and scoring machinery must not have a separate code path for "real tool" and
"recording", or the recording stops being evidence about the real thing.

Two deliberate differences.

There is no pacing and no rate budget: a lookup in a dict cannot trip a
limiter. The constructor still accepts `min_interval` and `budget` so callers
do not need to branch, and ignores them.

And a call the recording does not contain is an error, not an empty answer.
A fixture that returns `None` for an unrecorded request is exactly the bug
this project keeps finding in its own probes -- an absence dressed up as an
observation. `strict=True` (the default) raises `FixtureMiss`; `strict=False`
records a miss with status 0 and a `fixture_miss` marker so a batch run can
finish and report how much of it was actually replayed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agent.contract import Anomaly, Call, ContractLayer
from fixtures.format import call_key, canonical, fixture_dir, read_manifest


class FixtureMiss(LookupError):
    """A probe asked a question the recording never asked."""


class FixtureAdapter:
    """Replay a `fixtures/<tool>/<version>/traffic.jsonl` recording."""

    def __init__(
        self,
        run_id: str,
        tool: str,
        version: str,
        guards: list[Any] | None = None,
        runs_dir: str | Path = "runs",
        fixtures_root: str | Path = "fixtures",
        doc_spec: dict[str, Any] | None = None,
        strict: bool = True,
        min_interval: float = 0.0,   # accepted for contract parity; a dict lookup
        budget: Any = None,          # cannot trip a rate limiter
    ) -> None:
        self.run_id = run_id
        self.tool = tool
        self.version = version
        self.name = f"{tool}@{version}"
        self.strict = strict
        self.min_interval = 0.0
        self.budget = None

        d = fixture_dir(tool, version, fixtures_root)
        self.manifest = read_manifest(tool, version, fixtures_root)
        self.docs = (d / "docs.md").read_text()
        # The operations this recording is allowed to serve. Declared in the
        # manifest at capture time and enforced again here, because a fixture
        # is a file and files get edited: the capture driver's read-only rule
        # has to survive the trip to disk.
        self.allowed_operations = set(self.manifest.get("allowed_operations") or [])

        self.exchanges: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        # A secondary index for callers that name an operation and arguments
        # but not a path -- the lab-shaped aliases, mostly. One entry per
        # (op, request) may map to several paths: Frankfurter puts the date in
        # the path and the rest in the query, so two genuinely different
        # questions look identical from here. Those are kept as a list and the
        # ambiguity is raised rather than resolved by picking one.
        self._by_request: dict[str, list[dict[str, Any]]] = {}
        for line in (d / "traffic.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec["key"] in self.exchanges:
                raise ValueError(
                    f"{self.name}: two exchanges share the key {rec['key']}; one would be "
                    f"unreachable on replay. Re-derive the fixture with "
                    f"`python -m fixtures.renormalize`.")
            self.exchanges[rec["key"]] = rec
            self.order.append(rec["key"])
            self._by_request.setdefault(
                f"{rec['op']}\n{canonical(rec['request'])}", []).append(rec)

        # Interpretive response headers from the last call. The rate-limit
        # mismatch class lives entirely in headers -- x-ratelimit-limit and
        # friends are the contract -- so a replay that dropped them could not
        # represent that class at all.
        self.last_headers: dict[str, str] = {}
        self.contract = ContractLayer(doc_spec) if doc_spec else ContractLayer()
        self.guards = guards or []
        self.n = 0
        self.misses: list[dict[str, Any]] = []
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

    def _call(self, op: str, payload: dict[str, Any] | None,
              path: str | None = None) -> tuple[int, Any]:
        refusal = None
        if self.allowed_operations and op not in self.allowed_operations:
            refusal = (
                f"operation {op!r} is not in this fixture's allowed_operations "
                f"{sorted(self.allowed_operations)}"
            )
        if refusal is None:
            for g in self.guards:
                payload, refusal = g.before(op, payload)
                if refusal:
                    break
        if refusal:
            self.n += 1
            rec = {"n": self.n, "t": time.time(), "op": op, "request": payload,
                   "status": 0, "response": {"refused_by_guard": refusal}, "guard": refusal}
            self._write(rec)
            return 0, {"refused_by_guard": refusal}

        if path is not None:
            key = call_key(op, payload, path)
            hit = self.exchanges.get(key)
        else:
            # No path given: resolve through the request index, and refuse to
            # choose when the same arguments were recorded against several
            # paths. Guessing here would hand a probe the answer to a question
            # it did not ask, which is the one thing a fixture must never do.
            candidates = self._by_request.get(f"{op}\n{canonical(payload or {})}", [])
            if len(candidates) > 1:
                raise FixtureMiss(
                    f"{self.name}: {op} {payload!r} was recorded against "
                    f"{sorted(c['path'] for c in candidates)}. Pass path= to say which "
                    f"one you mean; answering with either would be answering a "
                    f"different question than the one asked.")
            hit = candidates[0] if candidates else None
            key = hit["key"] if hit else call_key(op, payload)
        if hit is None:
            miss = {"op": op, "request": payload, "key": key}
            self.misses.append(miss)
            if self.strict:
                raise FixtureMiss(
                    f"{self.name} has no recording for {op} {payload!r} (key {key}). "
                    f"A recording that answers an unrecorded question is not evidence."
                )
            self.n += 1
            self._write({"n": self.n, "t": time.time(), "op": op, "request": payload,
                         "status": 0, "response": {"fixture_miss": key}, "fixture_miss": True})
            return 0, {"fixture_miss": key}

        self.n += 1
        t = time.time()
        status, body = hit["status"], hit["response"]
        self.last_headers = dict(hit.get("headers") or {})
        rec = {
            "n": self.n, "t": t, "op": op, "request": payload,
            "status": status, "response": body,
            "headers": self.last_headers,
            "elapsed_ms": hit.get("elapsed_ms", 0.0),
            "replayed_from": f"{self.name}:{key}",
        }
        self._write(rec)

        found = self.contract.record(
            Call(n=self.n, op=op, request=payload or {}, status=status, response=body, t=t))
        self.anomalies.extend(found)

        for g in self.guards:
            body = g.after(op, payload, status, body, self)

        return status, body

    # --- documented surface -----------------------------------------------

    def call(self, op: str, _path: str | None = None, **params: Any) -> tuple[int, Any]:
        """The generic entry point: a fixture's operations are whatever it recorded.

        `_path` disambiguates when one operation and argument set were recorded
        against more than one path.
        """
        return self._call(op, params, path=_path)

    # Lab-shaped aliases, so anything written against LabAdapter also drives a
    # lab fixture. A fixture for a tool without these operations simply has no
    # recordings for them and misses loudly.
    def search(self, **params: Any) -> tuple[int, Any]:
        return self._call("search", params)

    def create(self, **fields: Any) -> tuple[int, Any]:
        return self._call("create", fields)

    def get(self, item_id: str) -> tuple[int, Any]:
        return self._call("get", {"id": item_id})

    def update(self, item_id: str, **fields: Any) -> tuple[int, Any]:
        return self._call("update", {"id": item_id, **fields})

    def bulk_create(self, items: list[dict[str, Any]]) -> tuple[int, Any]:
        return self._call("bulk_create", {"items": items})

    # --- reporting ---------------------------------------------------------

    def anomaly_signatures(self) -> list[str]:
        return sorted({f"{a.operation}.{a.parameter or '_'}.{a.kind}" for a in self.anomalies})

    def summary(self) -> dict[str, Any]:
        return {
            "adapter": "fixture",
            "tool": self.tool,
            "version": self.version,
            "calls": self.n,
            "recorded_exchanges": len(self.exchanges),
            "misses": len(self.misses),
            "anomalies": len(self.anomalies),
            "signatures": self.anomaly_signatures(),
            "wall_s": round(time.time() - self.t0, 2),
        }

    def replay_all(self) -> list[dict[str, Any]]:
        """Replay every recorded exchange in capture order.

        Used by `bench.replay_corpus` to exercise a whole fixture without
        knowing its operation names, and to check each response against the
        body hash recorded at capture time.
        """
        out: list[dict[str, Any]] = []
        for key in self.order:
            rec = self.exchanges[key]
            status, body = self._call(rec["op"], rec["request"], path=rec.get("path"))
            out.append({"key": key, "op": rec["op"], "status": status,
                        "body": body, "expected_body_sha256": rec.get("body_sha256")})
        return out

    def close(self) -> None:
        return None
