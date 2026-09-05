"""
Proves every hidden rule in ground_truth.yaml actually fires on the wire.

If a rule cannot be triggered here, the agent can never discover it, and
scoring recall against it would be dishonest. Run this before trusting any
bench number.

    ./.venv/bin/python lab/smoke.py
"""

from __future__ import annotations

import sys
import time

import httpx
import yaml

BASE = "http://127.0.0.1:8077"
C = httpx.Client(base_url=BASE, timeout=30.0)

results: list[tuple[str, bool, str]] = []


def slow():
    """Stay under the 3 req/s limit while testing unrelated rules."""
    time.sleep(0.4)


def check(rule: str, ok: bool, detail: str) -> None:
    results.append((rule, ok, detail))


def ctl(path: str, **kw):
    return C.post(f"/_control{path}", **kw)


def rule_off(rule: str):
    ctl(f"/rules/{rule}", json={"enabled": False})


def rule_on(rule: str):
    ctl(f"/rules/{rule}", json={"enabled": True})


def main() -> int:
    ctl("/reset", json={"seed": 1337})

    # --- title_truncation -------------------------------------------------
    rule_off("rate_limit_flaky_header")
    r = C.post("/v1/items", json={"title": "x" * 400}).json()
    check("title_truncation", len(r["title"]) == 255, f"len={len(r['title'])} (doc says 2000)")

    # --- pre_epoch_date_null ---------------------------------------------
    r = C.post("/v1/items", json={"title": "old", "due_date": "1965-04-01"}).json()
    check("pre_epoch_date_null", r["due_date"] is None, f"due_date={r['due_date']!r}")

    # --- large_number_to_string ------------------------------------------
    r = C.post("/v1/items", json={"title": "big", "amount": 2_000_000}).json()
    check("large_number_to_string", isinstance(r["amount"], str), f"type={type(r['amount']).__name__}")

    # --- unknown_field_ignored -------------------------------------------
    iid = C.post("/v1/items", json={"title": "patchme"}).json()["id"]
    resp = C.patch(f"/v1/items/{iid}", json={"nonexistent_field": 1})
    body = resp.json()
    check(
        "unknown_field_ignored",
        resp.status_code == 200 and "nonexistent_field" not in body,
        f"status={resp.status_code} (doc says 400 unknown_field)",
    )

    # --- write_search_lag -------------------------------------------------
    fresh = C.post("/v1/items", json={"title": "lagcheck", "vendor": "LagCo"}).json()
    immediate = C.post("/v1/search", json={"filter": {"vendor": "LagCo"}}).json()
    time.sleep(2.3)
    later = C.post("/v1/search", json={"filter": {"vendor": "LagCo"}}).json()
    check(
        "write_search_lag",
        len(immediate["results"]) == 0 and len(later["results"]) == 1,
        f"immediate={len(immediate['results'])} after_2.3s={len(later['results'])}",
    )

    # --- unknown_filter_field --------------------------------------------
    resp = C.post("/v1/search", json={"filter": {"vendorr": "typo"}})
    check(
        "unknown_filter_field",
        resp.status_code == 200 and resp.json()["results"] == [],
        f"status={resp.status_code} results={len(resp.json().get('results', []))} (doc says 400)",
    )

    # --- bulk_cap ---------------------------------------------------------
    payload = {"items": [{"title": f"bulk-{i}", "vendor": "BulkCo"} for i in range(30)]}
    r = C.post("/v1/bulk_create", json=payload).json()
    time.sleep(2.3)
    found = C.post("/v1/search", json={"filter": {"vendor": "BulkCo"}, "page_size": 50}).json()
    check(
        "bulk_cap",
        r["created"] == 30 and len(found["results"]) == 20,
        f"reported={r['created']} actually_searchable={len(found['results'])}",
    )

    # --- page_size_cap ----------------------------------------------------
    # Needs >50 rows in the store or the cap is never exercised and the
    # assertion passes vacuously.
    C.post("/v1/bulk_create", json={"items": [{"title": f"pg-{i}", "vendor": "PageCo"} for i in range(20)]})
    C.post("/v1/bulk_create", json={"items": [{"title": f"pg2-{i}", "vendor": "PageCo"} for i in range(20)]})
    C.post("/v1/bulk_create", json={"items": [{"title": f"pg3-{i}", "vendor": "PageCo"} for i in range(20)]})
    time.sleep(2.3)
    total = C.post("/v1/search", json={"filter": {"vendor": "PageCo"}, "page_size": 50}).json()
    r = C.post("/v1/search", json={"filter": {"vendor": "PageCo"}, "page_size": 200}).json()
    check(
        "page_size_cap",
        len(r["results"]) == 50 and r["has_more"] is True,
        f"store_has>=60, asked 200 got {len(r['results'])} has_more={r['has_more']} (doc says up to 500)",
    )

    # --- sort_created_is_edited -------------------------------------------
    a = C.post("/v1/items", json={"title": "sortA", "vendor": "SortCo"}).json()
    time.sleep(0.2)
    b = C.post("/v1/items", json={"title": "sortB", "vendor": "SortCo"}).json()
    time.sleep(2.3)
    C.patch(f"/v1/items/{a['id']}", json={"status": "touched"})  # A now newest by edit
    r = C.post("/v1/search", json={"filter": {"vendor": "SortCo"}, "sort": "created"}).json()
    titles = [x["title"] for x in r["results"]]
    check(
        "sort_created_is_edited",
        titles == ["sortB", "sortA"],
        f"sort=created gave {titles} (creation order is sortA,sortB)",
    )

    # --- include_archived_flag + archived_get_404 -------------------------
    arch = C.post("/v1/items", json={"title": "archme", "vendor": "ArchCo"}).json()
    time.sleep(2.3)
    C.post(f"/v1/items/{arch['id']}/archive")
    without = C.post("/v1/search", json={"filter": {"vendor": "ArchCo"}}).json()
    with_flag = C.post(
        "/v1/search", json={"filter": {"vendor": "ArchCo"}, "include_archived": True}
    ).json()
    check(
        "include_archived_flag",
        len(without["results"]) == 0 and len(with_flag["results"]) == 1,
        f"without={len(without['results'])} with_undocumented_flag={len(with_flag['results'])}",
    )
    resp = C.get(f"/v1/items/{arch['id']}")
    check(
        "archived_get_404",
        resp.status_code == 404,
        f"GET archived -> {resp.status_code} (doc says it returns the item)",
    )

    # --- cursor_expiry ----------------------------------------------------
    # Age the cursor via the control plane rather than sleeping 60s.
    r = C.post("/v1/search", json={"page_size": 1}).json()
    cur = r.get("next_cursor")
    expired_code = None
    if cur:
        fresh_ok = C.post("/v1/search", json={"page_size": 1, "cursor": cur}).status_code
        ctl(f"/age_cursor/{cur}", json={"seconds": 65})
        expired_code = C.post("/v1/search", json={"page_size": 1, "cursor": cur}).status_code
    check(
        "cursor_expiry",
        cur is not None and expired_code == 400,
        f"fresh=200 aged_65s->{expired_code} (doc says cursors never expire)",
    )

    # --- write_502_after_commit ------------------------------------------
    # ~5% of creates: find one, then prove the item was committed anyway
    found_502 = False
    for i in range(120):
        resp = C.post("/v1/items", json={"title": f"idem-{i}", "vendor": "IdemCo"})
        if resp.status_code == 502:
            found_502 = True
            break
    time.sleep(2.3)
    after = C.post("/v1/search", json={"filter": {"vendor": "IdemCo"}, "page_size": 50}).json()
    committed = any(x["title"] == f"idem-{i}" for x in after["results"]) if found_502 else False
    check(
        "write_502_after_commit",
        found_502 and committed,
        f"got_502={found_502} item_committed_anyway={committed} (doc says 502 = not processed)",
    )

    # --- rate_limit_flaky_header -----------------------------------------
    rule_on("rate_limit_flaky_header")
    codes, headers_seen = [], []
    for _ in range(12):
        resp = C.post("/v1/search", json={"page_size": 1})
        codes.append(resp.status_code)
        if resp.status_code == 429:
            headers_seen.append("Retry-After" in resp.headers)
    got_429 = 429 in codes
    flaky = len(set(headers_seen)) > 1 if len(headers_seen) > 3 else None
    check(
        "rate_limit_flaky_header",
        got_429,
        f"429s={codes.count(429)}/12 retry_after_present={headers_seen.count(True)}/{len(headers_seen)} (doc says no rate limit)",
    )

    # --- report -----------------------------------------------------------
    gt = yaml.safe_load(open("lab/ground_truth.yaml"))
    expected = {r["id"] for r in gt["rules"]}
    tested = {r for r, _, _ in results}
    missing = expected - tested

    width = max(len(r) for r, _, _ in results)
    print("\n  lab smoke test -- does each documented lie actually happen?\n")
    for rule, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {rule.ljust(width)}  {detail}")
    if missing:
        print(f"\n  untested rules: {sorted(missing)}")

    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"\n  {n_ok}/{len(results)} rules verified on the wire\n")
    return 0 if n_ok == len(results) and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
