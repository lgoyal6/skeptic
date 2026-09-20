"""
The capture driver: the one place in this project that calls a live service.

Everything downstream -- probes, benchmarks, tests, CI -- runs from what this
writes. That division is the point. A live call is fast to make and impossible
to review; a recording is reviewable, deterministic, free, and the same on
every machine. It is also the only way to ask a question about an API's past
behaviour after the API has changed.

Rules it enforces rather than documents:

1. **GET only, over https, unless the target is local.** A mutating call
   against somebody else's production service is not something a test suite
   gets to do by accident, so it is not a parameter a plan can set wrong.
   `local: true` is only for the lab -- this project's own disposable
   instrument on loopback.
2. **Redact before anything reaches disk**, and record what the scan found.
   The public tools here are credential-free, so it removes nothing; it runs
   anyway, because "no secrets were found" is only evidence if a search
   happened.
3. **Write the raw layer once and never edit it.** A correction is a new
   version. The normalized fixture is derived from raw by `normalize()`, and
   `verify()` re-derives it to prove nobody hand-edited the copy tests read.
4. **Refuse to write a fixture that does not verify.**

    ./.venv/bin/python -m fixtures.capture --plan fixtures/plans/open-meteo.yaml
    ./.venv/bin/python -m fixtures.capture --all
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from fixtures.format import (
    body_hash,
    content_hash,
    fixture_dir,
    normalize,
    raw_dir,
    redact,
    verify,
)

DRIVER = "skeptic.fixtures.capture"
DRIVER_VERSION = "2"
PLANS = Path("fixtures/plans")


def _fetch(client: httpx.Client, method: str, path: str,
           params: dict[str, Any] | None, local: bool) -> dict[str, Any]:
    method = method.upper()
    if method != "GET" and not local:
        raise ValueError(
            f"capture refuses {method} against a non-local target: fixtures are "
            f"built from read-only traffic, and a mutating call against somebody "
            f"else's service is not a thing a capture plan gets to request"
        )
    t = time.time()
    if method == "GET":
        r = client.get(path, params=params or None)
    elif method == "PATCH":
        r = client.patch(path, json=params or {})
    else:
        r = client.post(path, json=params or {})
    elapsed = round((time.time() - t) * 1000, 1)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"_raw": r.text[:4000]}
    return {"status": r.status_code, "headers": dict(r.headers),
            "response": body, "elapsed_ms": elapsed}


def capture(plan_path: str | Path, root: str | Path = "fixtures",
            offline_source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one capture plan: write the raw layer, derive the fixture, verify."""
    plan = yaml.safe_load(Path(plan_path).read_text())
    tool, version = plan["tool"], str(plan["version"])
    local = bool(plan.get("local", False))
    base = str(plan["base_url"])
    if not local and not base.startswith("https://"):
        raise ValueError(f"non-local capture target must be https: {base!r}")

    rd = raw_dir(tool, version, root)
    if rd.exists() and not plan.get("allow_recapture"):
        raise RuntimeError(
            f"raw capture {rd} already exists. Raw captures are immutable: a "
            f"correction creates a NEW version rather than rewriting the record "
            f"an earlier measurement was taken against. Bump `version`, or set "
            f"`allow_recapture: true` to deliberately replace an unreferenced one."
        )
    rd.mkdir(parents=True, exist_ok=True)

    allowed = sorted({str(c["op"]) for c in plan["calls"]})
    methods = sorted({str(c.get("method", "GET")).upper() for c in plan["calls"]})
    findings: list[str] = []
    lines: list[str] = []

    client = None
    if offline_source is None:
        client = httpx.Client(
            base_url=base, timeout=30.0, follow_redirects=True,
            headers={"user-agent": plan.get("user_agent", "skeptic-fixture-capture/2"),
                     **(plan.get("headers") or {})})
    try:
        for i, c in enumerate(plan["calls"], start=1):
            op, path = str(c["op"]), str(c["path"])
            params = dict(c.get("params") or {})
            if offline_source is not None:
                got = offline_source[(op, i)]
            else:
                got = _fetch(client, c.get("method", "GET"), path, params, local)
                time.sleep(float(plan.get("pace_s", 1.0)))  # be a polite guest

            clean_req, f1 = redact(params)
            clean_hdr, f2 = redact(got["headers"])
            clean_body, f3 = redact(got["response"])
            findings.extend(f1 + f2 + f3)

            lines.append(json.dumps({
                "n": i, "op": op, "method": str(c.get("method", "GET")).upper(),
                "path": path, "request": clean_req,
                "status": got["status"], "headers": clean_hdr,
                "response": clean_body, "elapsed_ms": got["elapsed_ms"],
                "why": c.get("why", ""),
                "body_sha256": body_hash(clean_body),
            }, sort_keys=True))
    finally:
        if client is not None:
            client.close()

    docs = plan.get("docs") or ""
    if plan.get("docs_file"):
        docs = Path(plan["docs_file"]).read_text()

    raw_capture = "\n".join(lines) + "\n"
    (rd / "capture.jsonl").write_text(raw_capture)
    (rd / "docs.snapshot").write_text(docs)
    (rd / "raw_manifest.json").write_text(json.dumps({
        "tool": tool, "version": version, "base_url": base,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "capture_driver": DRIVER, "capture_driver_version": DRIVER_VERSION,
        "immutable": True,
        "note": "Raw capture. Never edit in place; a correction creates a new version.",
    }, indent=2) + "\n")

    norm = normalize(tool, version, root)
    out = fixture_dir(tool, version, root)

    expected = plan.get("expected") or {"tool": tool, "version": version,
                                        "qualitative": True, "rules": []}
    (out / "expected.yaml").write_text(yaml.safe_dump(expected, sort_keys=False))

    manifest = {
        "tool": tool,
        "version": version,
        "endpoint": {"base_url": base,
                     "paths": sorted({str(c["path"]) for c in plan["calls"]})},
        "api_version": plan.get("api_version"),
        "docs_url": plan.get("docs_url"),
        "docs_sha256": content_hash(norm["docs"]),
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "capture_driver": DRIVER,
        "capture_driver_version": DRIVER_VERSION,
        "exchanges": norm["exchanges"],
        "allowed_operations": allowed,
        "methods_used": methods,
        "local_target": local,
        "credential_free": bool(plan.get("credential_free", True)),
        "license": plan["license"],
        "redaction": {
            "scanned": True,
            "patterns": "fixtures.format.SECRET_PATTERNS + SENSITIVE_HEADERS",
            "placeholder": "<redacted> (a constant, so a redaction is never itself a signal)",
            "findings": sorted(set(findings)),
        },
        "raw_capture_sha256": content_hash(raw_capture),
        "traffic_sha256": content_hash(norm["traffic"]),
        "qualitative": bool(expected.get("qualitative", False)),
        "mismatch_classes": sorted({str(r.get("mismatch_class", "")) for r
                                    in (expected.get("rules") or [])} - {""}),
        "replay_command": (
            f"./.venv/bin/python -m bench.replay_corpus --tool {tool} --version {version}"),
        "note": plan.get("note", ""),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    problems = verify(tool, version, root)
    if problems:
        raise RuntimeError(f"fixture written but does not verify: {problems}")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan")
    ap.add_argument("--all", action="store_true", help="run every plan in fixtures/plans")
    ap.add_argument("--root", default="fixtures")
    a = ap.parse_args()

    if not a.all and not a.plan:
        ap.error("pass --plan <file> or --all")
    plans = sorted(PLANS.glob("*.yaml")) if a.all else [Path(a.plan)]

    for p in plans:
        m = capture(p, root=a.root)
        print(f"  {m['tool']}@{m['version']}: {m['exchanges']} exchanges, "
              f"ops {m['allowed_operations']}, "
              f"classes {m['mismatch_classes'] or ['(control)']}")
        print(f"     redaction: {m['redaction']['findings'] or 'nothing found'}   "
              f"raw {m['raw_capture_sha256'][:19]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
