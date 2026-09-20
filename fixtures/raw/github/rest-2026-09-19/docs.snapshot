# GitHub REST API, documentation snapshot

Source: https://docs.github.com/en/rest
Captured: 2026-09-19
Authentication: none. Unauthenticated requests are rate limited to 60 per hour.

## Documented promises exercised by this capture

- `per_page`: "The number of results per page (max 100)." The documentation
  gives a maximum; it does not say what happens above it.
- Unauthenticated requests carry `x-ratelimit-limit`, `x-ratelimit-remaining`,
  `x-ratelimit-used`, `x-ratelimit-resource` and `x-ratelimit-reset` headers.
  `x-ratelimit-reset` is "the time at which the current rate limit window
  resets, in UTC epoch seconds."

## What a caller is entitled to assume

For `per_page`, nothing in particular above 100 -- which is the point. The
documentation states a maximum and stops, so a caller sending 200 has no
documented outcome to expect, and the observed outcome (a silent clamp to 100,
with nothing in the body or headers saying so) is the same shape as the lab's
page_size rule and GitHub's own is not announced the way MediaWiki's is.

For the rate limit, the headers say exactly what the documentation says. This
is recorded as a control: it is the same mismatch class as the lab's
`rate_limit_flaky_header` rule, behaving honestly, and a belief claiming an
undocumented rate limit here would be a false belief.

## What cannot be observed from a capture

`x-ratelimit-reset` promises that the window resets at a stated time. A
recording made in one second cannot observe an event an hour later. The
documented promise is real and the response reports it, but no probe against
this fixture can confirm or refute it, so the correct outcome is abstention.
