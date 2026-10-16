"""Bounded public demo endpoint for Skeptic's source date guard."""

from __future__ import annotations

from datetime import date
import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shim.corpus_guards import _echoed_date_matches_request


MAX_BODY_BYTES = 2048


def _valid_date(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def evaluate_exchange(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"requestDate", "responseDate", "status"}:
        raise ValueError("Send requestDate, responseDate, and status only.")
    request_date = payload["requestDate"]
    response_date = payload["responseDate"]
    status = payload["status"]
    if not _valid_date(request_date) or not _valid_date(response_date):
        raise ValueError("Enter valid request and response dates.")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("Enter an HTTP status from 100 to 599.")
    exchange = {
        "path": f"/v1/{request_date}",
        "status": status,
        "request": {"date": request_date},
        "response": {"date": response_date},
    }
    finding = _echoed_date_matches_request(exchange)
    return {
        "finding": finding,
        "checked": status == 200,
        "requestDate": request_date,
        "responseDate": response_date,
        "status": status,
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._send(405, {"error": "Use POST."})

    def do_POST(self):
        if self.path != "/api/date_guard":
            self._send(404, {"error": "Not found."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "Invalid request body."})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(413 if length > MAX_BODY_BYTES else 400, {"error": "Request body must be between 1 and 2,048 bytes."})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            result = evaluate_exchange(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "Request body must be valid JSON."})
            return
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(200, result)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
