"""
Re-derive every normalized fixture from its immutable raw capture.

    ./.venv/bin/python -m fixtures.renormalize

The raw layer is the evidence and is never edited. The normalized layer is a
pure function of it, so when that function changes -- a corrected lookup key,
a different set of interpretive headers -- the fixtures can be rebuilt without
going back to the network, and without a re-capture that would silently
replace the recording a published measurement was taken against.

Only the derived files and the two hashes in the manifest that describe them
are rewritten. `captured_at`, the raw hash, the licence and the answer key all
carry through untouched, because none of them are things normalization knows
anything about.
"""

from __future__ import annotations

import json
import sys

from fixtures.format import content_hash, fixture_dir, normalize, tools, verify


def main() -> int:
    bad = 0
    for tool, version in tools():
        norm = normalize(tool, version)
        mp = fixture_dir(tool, version) / "manifest.json"
        m = json.loads(mp.read_text())
        before = m.get("traffic_sha256")
        m["traffic_sha256"] = content_hash(norm["traffic"])
        m["docs_sha256"] = content_hash(norm["docs"])
        m["exchanges"] = norm["exchanges"]
        mp.write_text(json.dumps(m, indent=2) + "\n")

        problems = verify(tool, version)
        bad += bool(problems)
        moved = "rebuilt" if before != m["traffic_sha256"] else "unchanged"
        print(f"  {tool}@{version:<24} {norm['exchanges']:>3} exchanges  {moved}"
              + ("" if not problems else f"   PROBLEM: {problems}"))
    print(f"\n  {'all fixtures verify' if not bad else f'{bad} fixture(s) do not verify'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
