# Frankfurter v1, documentation snapshot

Source: https://frankfurter.dev, https://api.frankfurter.dev/v2/openapi.json
Captured: 2026-09-19
Authentication: "No authentication required. Open-source."

## Documented promises exercised by this capture

- `base`: the base currency for the returned rates.
- `symbols`: a comma-separated list of target currencies to filter to. The
  documentation describes it as a filter over which currencies are returned;
  it does not say what happens to a code that is not a currency.
- A dated request returns the rates for that date, echoed in a `date` field.
- Rates come from central bank publications, which publish on working days.

## What a caller is entitled to assume

That asking for a currency that does not exist is an error. The API proves it
can tell: an unknown `base` returns 404. An unknown entry in `symbols` returns
200 with the currency simply absent, so a caller who asked for three and got
two has no signal unless they count.

That the rates returned are for the date requested. When no rate was published
for that date the API returns the most recent published date instead, still
HTTP 200, and the only indication is that the echoed `date` differs.
