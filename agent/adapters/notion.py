"""
Adapter for the real Notion API.

Everything else in this project has only ever talked to the instrumented
local lab, whose "documentation" is a spec dict skeptic itself controls. That
proves the loop (detect contradiction -> hypothesize -> design an experiment
-> settle it) works when the ground truth is known in advance. It does not
prove the loop works against a tool skeptic did not author, whose docs were
written by someone else, for humans, with the usual gaps and optimism.

Notion is that proof. Its public docs (https://developers.notion.com) make
specific, checkable promises -- a ~3 req/s average rate limit, page_size
capped at 100 with next_cursor/has_more pagination, archived pages that
should behave the same in search and retrieve -- and its real behavior is
known (from outside this project) to sometimes disagree with them. If
skeptic can point at one of those disagreements using nothing but this
adapter's JSONL logs, that is evidence the method generalizes past its own
sandbox.

Same shape as adapters/lab.py on purpose: same per-call JSONL record keys,
same guard before/after hooks, same ContractLayer wiring, same self-pacing.
The only new piece is the write lock (allow_writes) -- lab's guards are all
opt-in objects the caller supplies, but here the caller is pointed at
somebody's real workspace, so refusing writes is the default rather than
something a guard has to be remembered.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from agent.contract import Anomaly, Call, ContractLayer
from agent.llm import load_env

BASE = "https://api.notion.com"
NOTION_VERSION = "2022-06-28"


# What developers.notion.com promises, in the same machine-readable shape as
# contract.DOC_SPEC. ContractLayer's checks were written against the lab's
# operation names and field names, so most of them will quietly no-op against
# these Notion calls (op names like "create_page" don't match lab's "create",
# there is no "amount" field, etc). That mismatch is expected, not a bug: the
# checks that DO line up (status codes, the page_size/has_more cap check,
# since Notion's real response shape happens to use the same "results" /
# "has_more" keys) still fire, and the rest of this spec exists so a future
# Notion-specific pass over contract.py has documented ground truth to check
# against rather than having to re-derive it from the docs again.
NOTION_DOC_SPEC: dict[str, Any] = {
    "tool": "notion",
    # https://developers.notion.com/reference/status-codes
    "status_codes": {
        "search": [200, 400, 401, 403, 429, 500, 503],
        "query_database": [200, 400, 401, 403, 404, 429, 500, 503],
        "get_page": [200, 400, 401, 403, 404, 429, 500, 503],
        "create_page": [200, 400, 401, 403, 404, 429, 500, 503],
        "update_page": [200, 400, 401, 403, 404, 409, 429, 500, 503],
        "block_children": [200, 400, 401, 403, 404, 429, 500, 503],
    },
    # https://developers.notion.com/reference/property-value-object -- the
    # shape each property type's own key is documented to hold.
    "field_types": {
        "title": "array<rich_text>",
        "rich_text": "array<rich_text>",
        "number": "number|null",
        "select": "object|null",
        "multi_select": "array<object>",
        "date": "object|null",
        "checkbox": "boolean",
        "url": "string|null",
        "email": "string|null",
        "formula": "object",
        "relation": "array<object>",
        "people": "array<object>",
        "status": "object|null",
    },
    "limits": {
        # https://developers.notion.com/reference/pagination -- "page_size":
        # up to 100, default 100. Applies to search, database query, and
        # block children alike.
        "page_size_max": 100,
        # https://developers.notion.com/reference/request-limits -- payload
        # ceilings: rich_text content capped at 2000 characters per element.
        "rich_text_max_chars": 2000,
    },
    "rate_limit": {
        # https://developers.notion.com/reference/request-limits -- documented
        # as "an average of three requests per second", enforced per
        # integration token, with 429 + Retry-After (seconds) on violation.
        # What's NOT specified: the burst allowance, whether limiting is a
        # sliding window or a bucket, and whether Retry-After is present on
        # every 429 or only some -- exactly the kind of gap this project is
        # meant to close experimentally rather than assume.
        "average_rps": 3,
        "documented_enforcement": "per integration, average not instantaneous",
        "documented_signal": "429 with Retry-After header",
    },
    "promises": {
        # https://developers.notion.com/reference/pagination
        "pagination_request_field": "start_cursor",
        "pagination_cursor_field": "next_cursor",
        "pagination_has_more_field": "has_more",
        # https://developers.notion.com/reference/post-search -- search is
        # documented as covering pages and databases shared with the
        # integration, and Notion's own guidance treats trashed/archived
        # content as excluded from a normal search.
        "search_excludes_archived": True,
        # https://developers.notion.com/reference/retrieve-a-page -- retrieve
        # is documented to return the Page object for the given id with no
        # mention of an archived-state filter, i.e. it should succeed
        # regardless of whether the page is archived.
        "get_page_returns_archived": True,
        # https://developers.notion.com/reference/patch-page -- updating
        # properties on an archived page is not documented as a distinct
        # error case.
        "update_page_archived_is_undocumented_case": True,
        # https://developers.notion.com/reference/get-block-children --
        # block children pagination uses the same start_cursor/next_cursor/
        # has_more contract as search and database query.
        "block_children_same_pagination_contract": True,
    },
}


def _headers(token: str | None) -> dict[str, str]:
    # Authorization is intentionally the only header this module ever builds
    # from a secret. It is never written to the JSONL log (see _call: only
    # `payload`, the JSON body/query params, is logged) and this function is
    # the one place a caller could go to print headers for debugging, so it
    # redacts on principle even though nothing here currently logs headers.
    return {
        "Authorization": f"Bearer {token or ''}",
        "Notion-Version": NOTION_VERSION,
        "content-type": "application/json",
    }


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Defensive helper for any future debug path that logs headers."""
    return {k: ("Bearer ***" if k.lower() == "authorization" else v) for k, v in headers.items()}


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class NotionAdapter:
    name = "notion"

    def __init__(
        self,
        run_id: str,
        base: str = BASE,
        guards: list[Any] | None = None,
        runs_dir: str | Path = "runs",
        min_interval: float = 0.0,
        token: str | None = None,
        allow_writes: bool = False,
    ) -> None:
        self.run_id = run_id
        # Same self-pacing knob as lab.py, same reason: an experiment that
        # fires calls back-to-back can trip Notion's ~3 req/s limit and then
        # reason about a 429 instead of whatever it actually meant to
        # measure. Default is 0.0 (caller opts in) so a probe designed to
        # measure the rate limit itself isn't accidentally paced around it.
        self.min_interval = min_interval
        self._last_call_t = 0.0

        load_env()
        self._token = token or os.environ.get("NOTION_TOKEN")
        self.client = httpx.Client(base_url=base, headers=_headers(self._token), timeout=30.0)

        self.contract = ContractLayer(NOTION_DOC_SPEC)
        self.guards = guards or []
        # Writes are refused by default. This adapter points at somebody's
        # real Notion workspace, not a disposable local lab -- opting into
        # create_page/update_page has to be a deliberate constructor choice,
        # not something left to whichever guards happen to be attached.
        self.allow_writes = allow_writes

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

    def _call(
        self,
        op: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        params: dict[str, Any] | None = None,
        requires_write: bool = False,
    ) -> tuple[int, Any]:
        # The write lock is checked before guards, and produces the exact
        # same refusal record shape lab.py uses for a guard refusal, so
        # downstream code (replay, summaries) can't tell the two apart.
        refusal = None
        if requires_write and not self.allow_writes:
            refusal = "writes disabled: construct NotionAdapter(allow_writes=True) to permit " + op
        if refusal is None:
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

        if self.min_interval > 0:
            gap = time.time() - self._last_call_t
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)

        self.n += 1
        t = time.time()
        self._last_call_t = t
        if method == "GET":
            r = self.client.get(path, params=params)
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

        for g in self.guards:
            body = g.after(op, payload, r.status_code, body, self)

        return r.status_code, body

    # --- documented surface -------------------------------------------------

    def search(
        self,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        page_size: int | None = None,
        start_cursor: str | None = None,
        sort: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        body = _drop_none({
            "query": query, "filter": filter, "page_size": page_size,
            "start_cursor": start_cursor, "sort": sort,
        })
        return self._call("search", "POST", "/v1/search", body)

    def query_database(
        self,
        database_id: str,
        filter: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
        page_size: int | None = None,
        start_cursor: str | None = None,
    ) -> tuple[int, Any]:
        body = _drop_none({
            "filter": filter, "sorts": sorts, "page_size": page_size, "start_cursor": start_cursor,
        })
        return self._call("query_database", "POST", f"/v1/databases/{database_id}/query", body)

    def get_page(self, page_id: str) -> tuple[int, Any]:
        return self._call("get_page", "GET", f"/v1/pages/{page_id}", {"id": page_id})

    def create_page(self, parent_database_id: str, properties: dict[str, Any]) -> tuple[int, Any]:
        body = {"parent": {"database_id": parent_database_id}, "properties": properties}
        return self._call("create_page", "POST", "/v1/pages", body, requires_write=True)

    def update_page(self, page_id: str, properties: dict[str, Any]) -> tuple[int, Any]:
        body = {"properties": properties}
        return self._call("update_page", "PATCH", f"/v1/pages/{page_id}", body, requires_write=True)

    def block_children(
        self,
        block_id: str,
        page_size: int | None = None,
        start_cursor: str | None = None,
    ) -> tuple[int, Any]:
        params = _drop_none({"page_size": page_size, "start_cursor": start_cursor})
        request = dict(params)
        request["id"] = block_id
        return self._call("block_children", "GET", f"/v1/blocks/{block_id}/children", request, params=params)

    # --- introspection ------------------------------------------------------

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


# Notion property values arrive as {"type": <name>, "id": ..., <name>: <value>}
# with a different shape of <value> per type -- exactly the kind of
# per-property-type variance the agent is meant to notice rather than assume
# away. This flattens that into plain values so the rest of the project can
# reason about "what does this page's Amount property say" without
# special-casing Notion's envelope everywhere.
def flatten_properties(page: dict[str, Any]) -> dict[str, Any]:
    props = (page or {}).get("properties") or {}
    return {name: _flatten_value(prop) for name, prop in props.items()}


def _flatten_value(prop: Any) -> Any:
    if not isinstance(prop, dict):
        return None
    ptype = prop.get("type")
    val = prop.get(ptype)

    if ptype in ("title", "rich_text"):
        if not val:
            return None
        text = "".join(seg.get("plain_text", "") for seg in val)
        return text or None

    if ptype == "number":
        return val

    if ptype in ("select", "status"):
        return val.get("name") if val else None

    if ptype == "multi_select":
        return [v.get("name") for v in val] if val else None

    if ptype == "date":
        if not val:
            return None
        cleaned = {k: v for k, v in val.items() if v is not None}
        return cleaned or None

    if ptype == "checkbox":
        return bool(val) if val is not None else None

    if ptype in ("url", "email"):
        return val

    if ptype == "formula":
        # {"type": "string"|"number"|"boolean"|"date", <type>: value}
        if not isinstance(val, dict):
            return None
        inner_type = val.get("type")
        return val.get(inner_type)

    if ptype == "relation":
        return [r.get("id") for r in val] if val else None

    if ptype == "people":
        return [p.get("name") or p.get("id") for p in val] if val else None

    # Unknown/unhandled property type: never raise, just hand back whatever
    # was under the type key so a caller can still inspect it.
    return val


def probe_ready() -> tuple[bool, str]:
    """(token present, trivial call succeeds) plus a human-readable reason.

    Must never raise -- this is the first thing anything calls to decide
    whether the Notion adapter can be exercised at all, and "no token
    configured" has to be a normal, quiet answer rather than an exception.
    """
    load_env()
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        return False, "NOTION_TOKEN not set"

    try:
        r = httpx.post(
            f"{BASE}/v1/search",
            headers=_headers(token),
            json={"page_size": 1},
            timeout=10.0,
        )
    except Exception as exc:
        return False, f"probe request failed: {exc!r}"

    if r.status_code == 200:
        return True, "NOTION_TOKEN present and POST /v1/search returned 200"
    if r.status_code == 401:
        return False, "NOTION_TOKEN present but rejected (401 unauthorized)"
    return False, f"NOTION_TOKEN present but probe returned HTTP {r.status_code}"
