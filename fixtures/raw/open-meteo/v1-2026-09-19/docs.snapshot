# Open-Meteo Forecast API, documentation snapshot

Source: https://open-meteo.com/en/docs
Captured: 2026-09-19

## Documented promises exercised by this capture

Quoted from the parameter table:

- `forecast_days`, `Integer (0-16)`, default `7`: "Per default, only 7 days are
  returned. Up to 16 days of forecast are possible."
- The response carries a `daily` object whose `time` array has one entry per day.
- An invalid request returns HTTP 400 with `{"error": true, "reason": "..."}`,
  where `reason` describes what was wrong with the request.

## What a caller is entitled to assume

That `reason` describes *this* request. A diagnostic exists to tell the caller
what they sent that the server would not accept. One that reports a value the
caller did not send is worse than no diagnostic, because it sends them to debug
the wrong thing -- and it is type-correct, so nothing downstream flags it.
