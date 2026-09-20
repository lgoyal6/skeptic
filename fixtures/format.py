"""
The fixture format: raw captures, normalized fixtures, keys, hashes, redaction.

Shared by the capture driver (which writes) and the fixture adapter (which
replays), so the two cannot drift. A recording whose key scheme differs by one
space between writer and reader replays as a total miss.

## Two layers, on purpose

    fixtures/raw/<tool>/<version>/     immutable, never edited in place
        capture.jsonl                  every exchange exactly as received
        docs.snapshot                  the documentation text, as fetched
        raw_manifest.json              provenance and hashes, written once

    fixtures/<tool>/<version>/         normalized, derived from raw
        docs.md
        traffic.jsonl
        expected.yaml
        manifest.json

The raw layer is the evidence. It keeps every response header and the body
verbatim, because which headers matter is a judgement made later, and a
capture that threw away the answer cannot be re-asked without going back to
the live service -- by which time the service has changed and the question is
unanswerable.

The normalized layer is what tests read. It is a pure function of the raw
layer, so `normalize()` can be re-run to produce it again, and the normalized
manifest carries the hash of the raw capture it came from. A correction never
edits a raw capture: it creates a new version. That is the same rule the
contract model uses for drift, for the same reason -- you cannot measure a
change against a record that was rewritten to match.

Redaction happens at capture, before anything reaches disk, so the raw layer
is safe to commit too.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

FIXTURES_ROOT = Path("fixtures")
RAW_ROOT = Path("fixtures/raw")

# Response headers that carry contract meaning and are kept in the normalized
# fixture. Everything else is preserved in the raw capture and dropped here:
# `date`, `age`, CDN trace ids and request ids change on every call, so keeping
# them would make two identical observations look like a contract change.
INTERPRETIVE_HEADERS = frozenset({
    "content-type", "retry-after", "link", "warning", "deprecation", "sunset",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-ratelimit-used", "x-ratelimit-resource", "ratelimit-limit",
    "ratelimit-remaining", "ratelimit-reset", "mediawiki-api-error",
})

SENSITIVE_HEADERS = frozenset({
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "x-api-key", "api-key", "x-auth-token", "notion-version-token",
})

# Values that look like credentials wherever they appear. Deliberately broad:
# a false positive costs one redacted string in a fixture, a false negative
# costs a published secret.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I)),
    ("notion_secret", re.compile(r"\bsecret_[A-Za-z0-9]{16,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}")),
    ("aws_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)

REDACTED = "<redacted>"


# ---------------------------------------------------------------------------
# canonicalisation and hashing
# ---------------------------------------------------------------------------


def canonical(obj: Any) -> str:
    """A stable text form, so the same content hashes the same.

    Sorted keys and no incidental whitespace. This is what makes "reordering
    irrelevant JSON fields must not create a contract change" true by
    construction rather than by a later comparison step remembering to sort:
    `{"a":1,"b":2}` and `{"b":2,"a":1}` produce identical text, identical
    hashes, and identical lookup keys.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def call_key(op: str, request: Any, path: str = "") -> str:
    """The lookup key for one recorded exchange.

    The path is part of the question. It was left out of the first version of
    this function, and Frankfurter v1 exposed why: `/v1/2026-09-13` and
    `/v1/2026-09-11` carry identical query parameters and differ only in the
    path segment holding the date. They hashed to the same key, the second
    overwrote the first in the replay index, and the recording quietly held
    four exchanges where the capture had made five -- including the one
    exchange that demonstrated the defect the fixture exists to record.

    Nothing failed. The fixture verified, the hashes matched, and the replay
    reported the right number of calls because it iterated a key list that
    contained the duplicate twice. A probe asking about the Sunday was handed
    the Friday's response.
    """
    return hashlib.sha256(
        f"{op}\n{path}\n{canonical(request or {})}".encode()).hexdigest()[:24]


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def body_hash(body: Any) -> str:
    """Hash of a response body, insensitive to key order."""
    return content_hash(canonical(body))


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def redact(value: Any) -> tuple[Any, list[str]]:
    """Strip anything credential-shaped, and report what was found.

    Returns the cleaned value and the pattern names that fired. The caller
    writes that list into the manifest: an empty list is the claim "this
    recording was scanned and held no secrets", which is only worth making
    because the scan is the thing that ran.

    Every redaction writes the same constant. A per-secret placeholder would
    make the redaction itself a distinguishing signal -- two exchanges that
    differed only in a stripped credential would still differ -- so the
    replacement carries no information about what it replaced.
    """
    found: list[str] = []

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            out = {}
            for k, sub in v.items():
                if str(k).lower() in SENSITIVE_HEADERS:
                    found.append(f"header:{str(k).lower()}")
                    out[k] = REDACTED
                else:
                    out[k] = walk(sub)
            return out
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            s = v
            for name, pat in SECRET_PATTERNS:
                if pat.search(s):
                    found.append(name)
                    s = pat.sub(REDACTED, s)
            return s
        return v

    return walk(value), sorted(set(found))


def interpretive(headers: dict[str, str]) -> dict[str, str]:
    """Keep only the response headers that carry contract meaning."""
    return {k.lower(): v for k, v in headers.items() if k.lower() in INTERPRETIVE_HEADERS}


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def fixture_dir(tool: str, version: str, root: Path | str = FIXTURES_ROOT) -> Path:
    return Path(root) / tool / version


def raw_dir(tool: str, version: str, root: Path | str = FIXTURES_ROOT) -> Path:
    return Path(root) / "raw" / tool / version


def read_manifest(tool: str, version: str, root: Path | str = FIXTURES_ROOT) -> dict[str, Any]:
    return json.loads((fixture_dir(tool, version, root) / "manifest.json").read_text())


def tools(root: Path | str = FIXTURES_ROOT) -> list[tuple[str, str]]:
    """Every (tool, version) in the corpus, excluding the raw layer."""
    out: list[tuple[str, str]] = []
    for m in sorted(Path(root).glob("*/*/manifest.json")):
        if "raw" in m.parts:
            continue
        out.append((m.parent.parent.name, m.parent.name))
    return out


# ---------------------------------------------------------------------------
# normalization: raw -> test fixture
# ---------------------------------------------------------------------------


def normalize(tool: str, version: str, root: Path | str = FIXTURES_ROOT) -> dict[str, Any]:
    """Derive the normalized fixture from the immutable raw capture.

    Pure function of the raw layer. Re-running it must reproduce the same
    bytes, which is what makes the normalized fixture checkable rather than
    merely present: if `traffic.jsonl` and a fresh `normalize()` disagree,
    somebody edited a derived file by hand.
    """
    rd = raw_dir(tool, version, root)
    raw_manifest = json.loads((rd / "raw_manifest.json").read_text())
    docs = (rd / "docs.snapshot").read_text()

    lines: list[str] = []
    for line in (rd / "capture.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        lines.append(json.dumps({
            "n": r["n"],
            "key": call_key(r["op"], r["request"], r.get("path", "")),
            "op": r["op"],
            "method": r["method"],
            "path": r["path"],
            "request": r["request"],
            "status": r["status"],
            "headers": interpretive(r.get("headers") or {}),
            "response": r["response"],
            "body_sha256": body_hash(r["response"]),
            "why": r.get("why", ""),
        }, sort_keys=True))

    out = fixture_dir(tool, version, root)
    out.mkdir(parents=True, exist_ok=True)
    traffic = "\n".join(lines) + "\n"
    (out / "traffic.jsonl").write_text(traffic)
    (out / "docs.md").write_text(docs)
    return {"traffic": traffic, "docs": docs, "raw_manifest": raw_manifest,
            "exchanges": len(lines)}


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def _collisions(traffic: str) -> int:
    lines = [json.loads(l) for l in traffic.splitlines() if l.strip()]
    return len(lines) - len({l["key"] for l in lines})


def verify(tool: str, version: str, root: Path | str = FIXTURES_ROOT) -> list[str]:
    """Check a fixture against its own manifest. Returns a list of problems.

    The manifest's whole purpose is to make "this run used this recording"
    checkable rather than assumed, which it only does if something checks it.
    """
    d = fixture_dir(tool, version, root)
    rd = raw_dir(tool, version, root)
    problems: list[str] = []
    if not d.is_dir():
        return [f"{d} does not exist"]
    try:
        m = read_manifest(tool, version, root)
    except Exception as e:  # noqa: BLE001
        return [f"manifest unreadable: {type(e).__name__}: {e}"]

    for name in ("docs.md", "traffic.jsonl", "expected.yaml", "manifest.json"):
        if not (d / name).is_file():
            problems.append(f"missing {name}")
    for name in ("capture.jsonl", "docs.snapshot", "raw_manifest.json"):
        if not (rd / name).is_file():
            problems.append(f"missing raw/{name}")

    for name, key in (("docs.md", "docs_sha256"), ("traffic.jsonl", "traffic_sha256")):
        p = d / name
        if not p.is_file():
            continue
        actual = content_hash(p.read_bytes())
        if m.get(key) != actual:
            problems.append(f"{name} hash mismatch: manifest {m.get(key)}, file {actual}")

    if (d / "traffic.jsonl").is_file():
        lost = _collisions((d / "traffic.jsonl").read_text())
        if lost:
            problems.append(
                f"{lost} exchange(s) share a lookup key with another and would be "
                f"unreachable on replay")

    if (rd / "capture.jsonl").is_file():
        actual = content_hash((rd / "capture.jsonl").read_bytes())
        if m.get("raw_capture_sha256") != actual:
            problems.append(
                f"raw capture hash mismatch: manifest {m.get('raw_capture_sha256')}, file {actual}")

    # The normalized layer must still be reproducible from raw. A hand-edited
    # traffic.jsonl is the failure this catches, and it is the one that would
    # otherwise be invisible: the file verifies against its own hash because
    # whoever edited it updated the hash too.
    if (rd / "capture.jsonl").is_file() and (d / "traffic.jsonl").is_file():
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            shadow = Path(td)
            (shadow / "raw" / tool / version).mkdir(parents=True)
            for f in ("capture.jsonl", "docs.snapshot", "raw_manifest.json"):
                if (rd / f).is_file():
                    (shadow / "raw" / tool / version / f).write_bytes((rd / f).read_bytes())
            try:
                again = normalize(tool, version, shadow)
                if again["traffic"] != (d / "traffic.jsonl").read_text():
                    problems.append("traffic.jsonl is not reproducible from the raw capture")
                if again["docs"] != (d / "docs.md").read_text():
                    problems.append("docs.md is not reproducible from the raw capture")
            except Exception as e:  # noqa: BLE001
                problems.append(f"re-normalization failed: {type(e).__name__}: {e}")

    for field in ("tool", "version", "endpoint", "docs_url", "docs_sha256",
                  "captured_at", "allowed_operations", "license", "replay_command",
                  "redaction", "raw_capture_sha256"):
        if field not in m:
            problems.append(f"manifest is missing required field {field!r}")
    if m.get("tool") != tool:
        problems.append(f"manifest tool {m.get('tool')!r} != directory {tool!r}")
    if m.get("version") != version:
        problems.append(f"manifest version {m.get('version')!r} != directory {version!r}")
    return problems
