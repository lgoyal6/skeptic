"""
Does the deterministic contract layer actually notice the lies?

lab/smoke.py proved the lab misbehaves. This proves the agent's detector sees
the misbehaviour without any model in the loop. If a rule cannot be detected
here, no amount of clever prompting downstream will find it, and recall
against that rule would be a fantasy.

    ./.venv/bin/python -m agent.detect_smoke
"""

from __future__ import annotations

import sys
import time

import httpx
import yaml

from agent.adapters.lab import LabAdapter

CTL = httpx.Client(base_url="http://127.0.0.1:8077", timeout=30.0)

# Which anomaly SIGNATURE each hidden rule must surface as.
#
# Signatures, not kinds. An earlier version of this test asserted on kinds and
# passed cursor_expiry vacuously, because 'undocumented_status' happened to
# appear elsewhere in the run while nothing fired for the cursor at all.
EXPECT = {
    "title_truncation": "create.title.silent_truncation",
    "pre_epoch_date_null": "create.due_date.silent_null",
    "large_number_to_string": "create.amount.type_coercion",
    "unknown_field_ignored": "update.nonexistent_field.silent_ignore",
    "write_search_lag": "search._.write_not_visible",
    "unknown_filter_field": "search.filter.silent_empty",
    "bulk_cap": "bulk_create.items.silent_truncation",
    "page_size_cap": "search.page_size.silent_truncation",
    "sort_created_is_edited": "search.sort.mislabelled_semantics",
    "include_archived_flag": "search.filter.silent_empty",
    "archived_get_404": "get._.undocumented_flag",
    "cursor_expiry": "search.cursor.expiry",
    "write_502_after_commit": "create._.idempotency_hazard",
    "rate_limit_flaky_header": "search._.undocumented_status",
}

# unknown_filter_field and include_archived_flag are indistinguishable at the
# anomaly layer: both present as "a filtered search came back empty". That is
# deliberate. One anomaly, two competing explanations, and only a designed
# probe can tell them apart -- which is the entire point of the project.
AMBIGUOUS = {"search.filter.silent_empty": ["unknown_filter_field", "include_archived_flag"]}


def main() -> int:
    CTL.post("/_control/reset", json={"seed": 1337})
    CTL.post("/_control/rules/rate_limit_flaky_header", json={"enabled": False})

    # A fixed path under /tmp is shared mutable state: two checkouts, two
    # parallel runs, or two users on one machine write the same file. Keep
    # scratch inside the checkout that produced it.
    a = LabAdapter(run_id="detect-smoke", runs_dir="runs/scratch")
    kinds: set[str] = set()

    def harvest():
        for an in a.anomalies:
            kinds.add(an.kind)

    # truncation / coercion / null on create
    a.create(title="x" * 400, vendor="DetectCo")
    a.create(title="old", due_date="1965-04-01", vendor="DetectCo")
    a.create(title="big", amount=2_000_000, vendor="DetectCo")

    # unknown field on update
    sc, it = a.create(title="patchme", vendor="DetectCo")
    if sc == 200:
        a.update(it["id"], nonexistent_field=1)

    # write not visible
    a.create(title="lag", vendor="LagCo")
    a.search(filter={"vendor": "LagCo"})

    # unknown filter field -> silent empty
    a.search(filter={"vendorr": "typo"})

    # bulk over cap
    a.bulk_create([{"title": f"b{i}", "vendor": "BulkCo"} for i in range(30)])
    time.sleep(2.3)
    a.search(filter={"vendor": "BulkCo"}, page_size=50)

    # page size cap (needs >50 rows)
    for _ in range(3):
        a.bulk_create([{"title": f"p{i}", "vendor": "PageCo"} for i in range(20)])
    time.sleep(2.3)
    a.search(filter={"vendor": "PageCo"}, page_size=200)

    # sort semantics
    sc, s1 = a.create(title="sA", vendor="SortCo")
    time.sleep(0.2)
    a.create(title="sB", vendor="SortCo")
    time.sleep(2.3)
    a.update(s1["id"], status="touched")
    a.search(filter={"vendor": "SortCo"}, sort="created")

    # archived: hidden from search, 404 on get
    sc, ar = a.create(title="arch", vendor="ArchCo")
    time.sleep(2.3)
    a.archive(ar["id"])
    a.search(filter={"vendor": "ArchCo"})
    a.get(ar["id"])

    # cursor expiry
    sc, r = a.search(page_size=1)
    cur = (r or {}).get("next_cursor")
    if cur:
        CTL.post(f"/_control/age_cursor/{cur}", json={"seconds": 65})
        a.search(page_size=1, cursor=cur)

    # 502 that committed anyway
    for i in range(120):
        sc, _ = a.create(title=f"idem-{i}", vendor="IdemCo")
        if sc == 502:
            break
    time.sleep(2.3)
    a.search(filter={"vendor": "IdemCo"}, page_size=50)

    # rate limit
    CTL.post("/_control/rules/rate_limit_flaky_header", json={"enabled": True})
    for _ in range(10):
        a.search(page_size=1)

    seen_sigs = {an.signature() for an in a.anomalies}

    gt = yaml.safe_load(open("lab/ground_truth.yaml"))
    rules = [r["id"] for r in gt["rules"]]

    print("\n  contract layer -- which lies does the detector actually see?\n")
    width = max(len(r) for r in rules)
    hit = 0
    for rid in rules:
        want = EXPECT[rid]
        ok = want in seen_sigs
        hit += ok
        amb = "  (ambiguous, needs a probe)" if want in AMBIGUOUS else ""
        print(f"  [{'SEEN' if ok else 'MISS'}] {rid.ljust(width)}  {want}{amb}")

    print(f"\n  distinct signatures observed: {len(seen_sigs)}")
    for s in sorted(seen_sigs):
        print(f"    {s}")
    print(f"\n  {hit}/{len(rules)} hidden rules produce a detectable anomaly")
    print(f"  {len(a.anomalies)} anomalies across {a.n} calls")
    print(f"  {len(AMBIGUOUS)} signature is shared by 2 rules and must be split by a probe\n")
    a.close()
    return 0 if hit == len(rules) else 1


if __name__ == "__main__":
    sys.exit(main())
