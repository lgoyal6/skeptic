"""
eventd: AO's event stream -> the orchestrator's inbox.

Without this, an "orchestrator" is a shell script in a while-loop that calls
`ao spawn` and polls for completion. With it, AO's own lifecycle events wake
the orchestrator agent: a worker finishing, a PR opening, CI going red, a
session asking for input.

AO's daemon publishes Server-Sent Events at /api/v1/events with Last-Event-ID
replay, so a restart resumes rather than missing what happened while it was
down. `ao events` is not exposed on the CLI yet, but the daemon route ships,
so we read it over HTTP directly.

    python -m ao.eventd --session <orchestrator-session-id>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import httpx

DAEMON = os.environ.get("AO_BASE", "http://127.0.0.1:3001")
CURSOR = Path(".ao-event-cursor")

# Events worth waking a planning agent for. Everything else is noise.
INTERESTING = {
    "session.completed",
    "session.needs_input",
    "session.failed",
    "pr.opened",
    "pr.merged",
    "pr.closed",
    "pr.checks_failed",
    "pr.review_submitted",
}


def stream(last_id: str | None = None) -> Iterator[dict[str, Any]]:
    headers = {"Accept": "text/event-stream"}
    if last_id:
        headers["Last-Event-ID"] = last_id
    with httpx.stream("GET", f"{DAEMON}/api/v1/events", headers=headers, timeout=None) as r:
        r.raise_for_status()
        event_id, data_lines = None, []
        for line in r.iter_lines():
            if line.startswith("id:"):
                event_id = line[3:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "":
                if data_lines:
                    raw = "\n".join(data_lines)
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = {"raw": raw}
                    yield {"id": event_id, **payload}
                event_id, data_lines = None, []


def describe(ev: dict[str, Any]) -> str:
    kind = ev.get("type") or ev.get("kind") or "event"
    sid = ev.get("session_id") or ev.get("sessionId") or ""
    pr = ev.get("pr_number") or ev.get("prNumber") or ""
    bits = [f"AO event: {kind}"]
    if sid:
        bits.append(f"session={sid}")
    if pr:
        bits.append(f"pr=#{pr}")
    return " ".join(bits)


def send(session: str, text: str) -> None:
    subprocess.run(["ao", "send", session, text], check=False,
                   capture_output=True, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True, help="orchestrator session id")
    ap.add_argument("--all", action="store_true", help="forward every event, not just interesting ones")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    last = CURSOR.read_text().strip() if CURSOR.exists() else None
    print(f"eventd -> {a.session} (resuming from {last or 'now'})", flush=True)

    while True:
        try:
            for ev in stream(last):
                if ev.get("id"):
                    last = ev["id"]
                    CURSOR.write_text(last)
                kind = ev.get("type") or ev.get("kind") or ""
                if not a.all and kind not in INTERESTING:
                    continue
                msg = describe(ev)
                print(f"  {msg}", flush=True)
                if not a.dry_run:
                    send(a.session, msg + "\n\nDecide the next action and take it.")
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001 - a dropped stream is normal
            print(f"  stream dropped ({type(e).__name__}: {e}); reconnecting", flush=True)
            time.sleep(2.0)


if __name__ == "__main__":
    sys.exit(main())
