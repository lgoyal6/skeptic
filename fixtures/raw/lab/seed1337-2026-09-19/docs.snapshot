# Lab tool, documentation snapshot

Source: `lab/DOCS.md` in this repository.
Captured: 2026-09-19, seed 1337, default port 8077.

## Documented promises exercised by this capture

- `page_size`: "Any integer from **1 to 500**."
- `due_date`: any ISO date, stored and echoed as sent.
- `bulk_create`: accepts up to 100 items.
- Created items are immediately searchable.

## Why this fixture exists

The lab already has a full answer key in `lab/ground_truth.yaml`, so it is the
one tool where a fixture's fidelity can be checked against something other than
itself: a belief learned from this recording must match the same rules a belief
learned from the live lab matches. It is the control for the fixture path.

The seeding calls are part of the recording on purpose. The lab resets to zero
rows, and a page-size sweep against an empty store returns 0 for every value --
not evidence of a cap, and the bug this project has now found five times in its
own code. A capture that did not seed would record an experiment that could not
have observed its own variable.
