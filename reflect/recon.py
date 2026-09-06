"""
Reconnaissance: go looking for the lies, rather than waiting to trip over one.

The first version of this system learned only when a task run happened to
stumble into an anomaly. It usually did not. In one session the agent
searched with the default page size, got exactly what it asked for, invented
a garbage cursor, received a documented 400, and gave up -- three calls, zero
anomalies, nothing learned. Learning was gated on the agent being unlucky in
a productive way.

That is backwards for a system whose claim is that it goes looking. So recon
spends a bounded budget deliberately testing what the documentation PROMISES.
Every promise is a checkable assertion; the ones that fail are exactly the
places the docs lie.

This is the cheap, broad sweep. The probe designer is the narrow, expensive
follow-up that settles what recon surfaces.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from agent.adapters.lab import LabAdapter
from agent.contract import Anomaly

# Documented promises from lab/DOCS.md, each with an experiment that would
# catch it out. Ordered cheapest-first so a small budget still covers ground.
PROMISES: list[tuple[str, str, int]] = [
    ("title_accepts_2000_chars", "create an item with a 400-character title and read it back", 1),
    ("amount_is_always_number", "create an item with a very large amount", 1),
    ("due_date_accepts_any_iso", "create an item dated before 1970", 1),
    ("unknown_update_field_400", "PATCH a field the schema does not define", 2),
    ("unknown_filter_field_400", "search on a misspelled filter field", 1),
    ("page_size_up_to_500", "ask for more rows than the cap allows", 2),
    ("immediately_searchable", "create then immediately search for it", 2),
    ("bulk_up_to_100", "submit more items than the bulk cap", 2),
    ("get_returns_archived", "archive an item then fetch it by id", 3),
    ("sort_created_is_creation", "create in order, edit the oldest, then sort by created", 6),
    ("no_rate_limit", "send a rapid burst", 8),
    ("cursors_never_expire", "issue a cursor, let it age, reuse it", 3),
]


@dataclass
class ReconResult:
    calls: int
    wall_s: float
    anomalies: list[Anomaly]
    promises_tested: list[str]
    promises_broken: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "wall_s": round(self.wall_s, 2),
            "anomalies": len(self.anomalies),
            "signatures": sorted({a.signature() for a in self.anomalies}),
            "promises_tested": self.promises_tested,
            "promises_broken": self.promises_broken,
        }


def sweep(
    run_id: str = "recon",
    budget_calls: int = 40,
    runs_dir: str = "runs",
    vendor: str = "Recon",
    ctl_base: str = "http://127.0.0.1:8077",
) -> ReconResult:
    """Test each documented promise until the call budget runs out."""
    import httpx

    t0 = time.time()
    a = LabAdapter(run_id=run_id, runs_dir=runs_dir, min_interval=0.4)
    tested: list[str] = []
    broken: list[str] = []

    def budget_left() -> int:
        return budget_calls - a.n

    def note(name: str) -> None:
        tested.append(name)

    try:
        # --- cheap single-call promises -----------------------------------
        if budget_left() >= 1:
            note("title_accepts_2000_chars")
            sc, body = a.create(title="R" * 400, vendor=vendor)
            if isinstance(body, dict) and len(str(body.get("title", ""))) < 400:
                broken.append("title_accepts_2000_chars")

        if budget_left() >= 1:
            note("amount_is_always_number")
            sc, body = a.create(title=f"{vendor}-big", amount=2_000_000, vendor=vendor)
            if isinstance(body, dict) and isinstance(body.get("amount"), str):
                broken.append("amount_is_always_number")

        if budget_left() >= 1:
            note("due_date_accepts_any_iso")
            sc, body = a.create(title=f"{vendor}-old", due_date="1965-04-01", vendor=vendor)
            if isinstance(body, dict) and body.get("due_date") is None:
                broken.append("due_date_accepts_any_iso")

        if budget_left() >= 2:
            note("unknown_update_field_400")
            sc, body = a.create(title=f"{vendor}-patch", vendor=vendor)
            if sc == 200 and isinstance(body, dict):
                sc2, _ = a.update(body["id"], not_a_real_field=1)
                if sc2 == 200:
                    broken.append("unknown_update_field_400")

        if budget_left() >= 1:
            note("unknown_filter_field_400")
            sc, body = a.search(filter={"vendorr": vendor})
            if sc == 200:
                broken.append("unknown_filter_field_400")

        # --- needs volume --------------------------------------------------
        if budget_left() >= 4:
            note("bulk_up_to_100")
            sc, body = a.bulk_create([{"title": f"{vendor}-b{i}", "vendor": vendor} for i in range(30)])
            reported = (body or {}).get("created")
            ids = (body or {}).get("ids") or []
            if isinstance(reported, int) and reported > len(ids):
                broken.append("bulk_up_to_100")
            # The page_size check below needs more rows than the cap, or it
            # passes vacuously -- the same vacuous-pass trap the lab smoke
            # test hit. Two more batches puts the vendor comfortably over 50.
            for k in range(2):
                a.bulk_create([{"title": f"{vendor}-c{k}{i}", "vendor": vendor} for i in range(20)])
            time.sleep(2.3)

        if budget_left() >= 2:
            note("page_size_up_to_500")
            sc, body = a.search(filter={"vendor": vendor}, page_size=500)
            got = len((body or {}).get("results", []))
            if (body or {}).get("has_more") and got < 500:
                broken.append("page_size_up_to_500")

        if budget_left() >= 2:
            note("immediately_searchable")
            sc, made = a.create(title=f"{vendor}-fresh", vendor=f"{vendor}Fresh")
            sc2, found = a.search(filter={"vendor": f"{vendor}Fresh"})
            if sc == 200 and not (found or {}).get("results"):
                broken.append("immediately_searchable")

        if budget_left() >= 3:
            note("get_returns_archived")
            sc, made = a.create(title=f"{vendor}-arch", vendor=f"{vendor}Arch")
            if sc == 200 and isinstance(made, dict):
                time.sleep(2.3)
                a.archive(made["id"])
                sc2, _ = a.get(made["id"])
                if sc2 == 404:
                    broken.append("get_returns_archived")
                sc3, seen = a.search(filter={"vendor": f"{vendor}Arch"})
                if sc3 == 200 and not (seen or {}).get("results"):
                    broken.append("archived_hidden_from_search")

        if budget_left() >= 6:
            note("sort_created_is_creation")
            ids = []
            for i in range(3):
                sc, b = a.create(title=f"{vendor}-o{i}", vendor=f"{vendor}Sort")
                if sc == 200 and isinstance(b, dict):
                    ids.append(b["id"])
                time.sleep(0.12)
            time.sleep(2.3)
            if ids:
                a.update(ids[0], status="touched")
            sc, body = a.search(filter={"vendor": f"{vendor}Sort"}, sort="created")
            rows = (body or {}).get("results", [])
            created = [r.get("created") for r in rows]
            if len(created) >= 2 and created != sorted(created):
                broken.append("sort_created_is_creation")

        if budget_left() >= 3:
            note("cursors_never_expire")
            sc, body = a.search(filter={"vendor": vendor}, page_size=1)
            cur = (body or {}).get("next_cursor")
            if cur:
                httpx.post(f"{ctl_base}/_control/age_cursor/{cur}",
                           json={"seconds": 65}, timeout=10.0)
                sc2, b2 = a.search(filter={"vendor": vendor}, page_size=1, cursor=cur)
                if sc2 == 400 and (b2 or {}).get("error") == "cursor_expired":
                    broken.append("cursors_never_expire")

        # --- burst last, it is the most disruptive -------------------------
        if budget_left() >= 8:
            note("no_rate_limit")
            a.min_interval = 0.0
            codes = []
            for _ in range(8):
                sc, _ = a.search(page_size=1)
                codes.append(sc)
            if 429 in codes:
                broken.append("no_rate_limit")
            a.min_interval = 0.4

    finally:
        anomalies = list(a.anomalies)
        calls = a.n
        a.close()

    return ReconResult(
        calls=calls,
        wall_s=time.time() - t0,
        anomalies=anomalies,
        promises_tested=tested,
        promises_broken=broken,
    )
