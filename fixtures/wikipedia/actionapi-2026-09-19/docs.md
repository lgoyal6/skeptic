# MediaWiki Action API (English Wikipedia), documentation snapshot

Source: https://www.mediawiki.org/wiki/API:Search
Captured: 2026-09-19
Authentication: none. Etiquette requires a descriptive User-Agent.

## Documented promises exercised by this capture

- `srlimit`: how many total pages to return. Documented as "The value must be
  between 1 and 500" for unprivileged users.
- Out-of-range parameter values produce a `warnings` object in the response
  alongside the results.

## Why this fixture is the control

Three APIs in this corpus clamp a page-size parameter. The lab clamps
`page_size` to 50 silently. GitHub clamps `per_page` to 100 silently.
MediaWiki clamps `srlimit` to 500 and returns

    "warnings": {"search": {"*": "The value \"5000\" for parameter \"srlimit\"
     must be between 1 and 500."}}

in the same response. The clamp is identical; the honesty is not. That makes
this the negative control for the whole class: a pipeline that mints
`silent_truncation` here is wrong, because nothing was silent. It also echoes
the caller's actual value, "5000", which is the thing Open-Meteo gets wrong.
