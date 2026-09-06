"""
The UI server: reads artifacts, serves one page.

Deliberately thin. Everything shown here is read from files the loop already
writes -- beliefs/lab.yaml, runs/history.jsonl, probes/*.json,
bench/ab_result.json. Nothing is computed for display only, so the screen
cannot disagree with the bench.

    ./.venv/bin/python -m ui.server        # then: ao preview http://127.0.0.1:8099
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from agent.beliefs import BeliefStore, Status

app = FastAPI(title="skeptic", docs_url=None, redoc_url=None)
ROOT = Path(__file__).resolve().parent


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (ROOT / "index.html").read_text()


@app.get("/api/beliefs")
async def beliefs() -> JSONResponse:
    store = BeliefStore("lab")
    gt_classes: dict[str, str] = {}
    gt_path = Path("lab/ground_truth.yaml")
    if gt_path.exists():
        for r in (yaml.safe_load(gt_path.read_text()) or {}).get("rules", []):
            gt_classes[r["id"]] = r["truth"]

    rows = []
    for b in store.ordered():
        rows.append({
            "id": b.id,
            "status": b.status.value,
            "cls": b.cls,
            "operation": b.operation,
            "parameter": b.parameter,
            "doc_claims": b.doc_claims,
            "belief": b.belief,
            "action": b.action,
            "p_doc": round(b.posterior.p_doc_correct, 3),
            "alpha": round(b.posterior.alpha, 2),
            "beta": round(b.posterior.beta, 2),
            "evidence_for": b.evidence_for,
            "evidence_against": b.evidence_against,
            "probes": b.probes,
            "survived": b.survived_falsification,
            "competing": b.competing,
            "history": b.history[-14:],
            "replay": b.replay,
        })
    return JSONResponse({
        "counts": store.counts(),
        "beliefs": rows,
        "context_tokens": store.context_tokens(),
        "context_block": store.render_for_context(),
    })


@app.get("/api/score")
async def score() -> JSONResponse:
    try:
        from bench.score import score as _score
        return JSONResponse(_score())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)})


@app.get("/api/history")
async def history() -> JSONResponse:
    p = Path("runs/history.jsonl")
    if not p.exists():
        return JSONResponse({"runs": []})
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return JSONResponse({"runs": rows})


@app.get("/api/probes")
async def probes() -> JSONResponse:
    out: list[dict[str, Any]] = []
    for f in sorted(Path("probes").glob("*.json"), key=lambda x: x.stat().st_mtime):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:  # noqa: BLE001
            continue
    return JSONResponse({"probes": out[-25:]})


@app.get("/api/ab")
async def ab() -> JSONResponse:
    p = Path("bench/ab_result.json")
    if not p.exists():
        return JSONResponse({"available": False})
    return JSONResponse({"available": True, **json.loads(p.read_text())})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning")
