# Open Library Search API, documentation snapshot

Source: https://openlibrary.org/dev/docs/api/search
Captured: 2026-09-19

## Documented promises exercised by this capture

- `offset` / `limit`: "Use for pagination."
- `page` / `limit`: "Use for pagination, with limit corresponding to the page
  size. Note page starts at 1."
- The response carries `numFound`, `numFoundExact`, `start`, and `docs`.

The documentation states no ceiling on `limit`, and says nothing about `limit=0`.

## What a caller is entitled to assume

That a 200 with an empty `docs` array means the query matched nothing. That is
why `numFound` and `docs` are separate fields: one is the size of the result
set, the other is the page. A page size of zero produces an empty page for a
query matching tens of thousands of records, and the only thing distinguishing
it from "no matches" is a field the caller has no particular reason to read.
