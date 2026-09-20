# Lab tool, rate-limit capture, documentation snapshot

Source: `lab/DOCS.md` in this repository.
Captured: 2026-09-19, seed 1337.

## Documented promises exercised by this capture

`lab/DOCS.md` documents **no rate limit**. There is no documented 429, no
documented Retry-After, and no documented request budget.

## Why this capture exists

The corpus needs the rate-limit class as an actual mismatch, not only as a
control. GitHub's fixture is the control: its limit is documented and its
headers report it honestly. This is the opposite -- a limit that is not
documented at all, discovered only by hitting it.

It is also the one class that cannot be captured politely from a third party.
Establishing that an undocumented rate limit exists requires deliberately
exceeding it, which is a reasonable thing to do to your own disposable
instrument on loopback and an unreasonable thing to do to somebody's
production service. So this capture is local by necessity, not convenience,
and `pace_s` is 0 because pacing around the limiter would prevent the
measurement.

## What makes the header interesting

The 429s do not agree with each other. `Retry-After` is present on roughly
60% of them, so a client that reads the header on its first rejection and
concludes the API always provides one will be wrong on the next. That is a
harder defect than a missing header: the behaviour is inconsistent rather
than absent, so a single observation cannot characterise it.
