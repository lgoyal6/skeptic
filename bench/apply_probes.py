"""
Apply saved probe records to the belief store.

Experiments are expensive -- a probe costs real calls and minutes of model
time -- so their results must survive the process that produced them. The
first settle run proved the point: nine probes completed, two later ones
timed out, the run died, and every verdict was lost because they were only
applied at the end.

Probe records are written to probes/*.json as soon as they are judged. This
reads them back and applies them, so the work is durable and re-runnable.
Applying the same record twice is harmless: the verdict is the same, and the
posterior update is idempotent per probe id.

    ./.venv/bin/python -m bench.apply_probes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent.beliefs import BeliefStore
from reflect.probe import ProbeRecord, _apply


def load(path: Path) -> ProbeRecord | None:
    try:
        d = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None
    fields = {
        "id", "belief_ids", "template", "params", "why", "predictions",
        "observation", "verdicts", "learned", "calls", "wall_s", "adversarial",
        "learned_class", "learned_parameter", "learned_action",
    }
    kept = {k: v for k, v in d.items() if k in fields}
    required = {"id", "belief_ids", "template", "params", "why", "predictions",
                "observation", "verdicts", "learned", "calls", "wall_s"}
    if not required.issubset(kept):
        return None   # not a probe record
    return ProbeRecord(**kept)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", default="probes")
    ap.add_argument("--beliefs", default="beliefs")
    a = ap.parse_args()

    store = BeliefStore("lab", root=a.beliefs)
    # Only probe records. probes/ also holds falsification.json, the
    # adversary's review, which is a different shape entirely and used to
    # crash the loop partway through, silently leaving later records unapplied.
    files = sorted(Path(a.probes).glob("probe-*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        print("  no probe records found")
        return 0

    applied = 0
    seen_ids: set[str] = set()
    for f in files:
        rec = load(f)
        if rec is None or rec.id in seen_ids:
            continue
        seen_ids.add(rec.id)
        learned = {
            "learned": rec.learned,
            "learned_class": getattr(rec, "learned_class", ""),
            "learned_parameter": getattr(rec, "learned_parameter", ""),
            "learned_action": getattr(rec, "learned_action", ""),
        }
        try:
            _apply(store, rec, adversarial=rec.adversarial, learned=learned)
            applied += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! {f.name}: {type(e).__name__}: {e}")

    merged = store.dedupe()
    store.save()
    print(f"  applied {applied} probe records"
          + (f", merged {merged} duplicate belief(s)" if merged else ""))
    print(f"  beliefs: {store.counts()}")
    for b in store.active():
        print(f"    + [{b.cls}] {b.belief[:88]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
