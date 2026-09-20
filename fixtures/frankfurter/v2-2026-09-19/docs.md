# Frankfurter v2, documentation snapshot

Source: https://api.frankfurter.dev/v2/openapi.json
Captured: 2026-09-19
Authentication: "No authentication required. Open-source."

## Documented promises exercised by this capture

From the published OpenAPI description of `GET /v2/rates`:

- "Returns exchange rates blended across providers. Without date params,
  returns the latest rates. Each record is a single currency pair."
- `date`: "Specific date (YYYY-MM-DD). Cannot be combined with from/to."
- `base`: "Base currency (default: EUR)"
- `quotes`: "Comma-separated list of quote currencies to include" -- the v2
  spelling of what v1 called `symbols`.
- Documented response codes include `422`.

## What changed between v1 and v2

Three contract changes, two of them from a silent outcome to a loud one:

- The filter parameter is renamed: `symbols` becomes `quotes`.
- An unknown currency in `quotes` returns `422 {"status": 422, "message":
  "invalid currency: ZZZ"}`, where v1 returned 200 with it quietly missing.
  v2 treats an unknown `base` the same way, so the two parameters now agree.
- A date with no published rate returns the requested date. 2026-09-13 comes
  back as 2026-09-13, where v1 returned 2026-09-11.

A belief learned against v1 about either behaviour is false here. Capturing
both is what makes the demotion measurable rather than asserted.
