# REST Countries v3.1, documentation snapshot

Source: https://restcountries.com/
Captured: 2026-09-19
Authentication: none.

## Documented promises exercised by this capture

- `/v3.1/alpha/{code}` returns the country matching an ISO alpha code.
- `?fields=` selects which fields the response carries; the documentation
  describes it as a filter over the returned object.
- Standard HTTP semantics: 200 means the request succeeded.

## What was actually observed

Every request in this capture returns **HTTP 200** with

    {"success": false, "data": null,
     "errors": [{"message": "This API version has been deprecated. Please visit
      .../legacy-api-deprecation to migrate to our new version (v5)."}]}

The request did not succeed. The API version is retired. The transport layer
says 200 OK, and a client that checks the status code and then reads
`data` gets `null` -- which, for a field-selection probe, is indistinguishable
from "the server returned the object with every selected field empty". This is
the same failure this project found inside its own probe templates, where a
502 error body was read as "every field was silently nulled", except here the
status code actively endorses the misreading.

A second, independent defect: the remediation says to migrate to v5, and the
v5 path returns the identical message telling the caller to migrate to v5.

## What cannot be observed

The documented `?fields=` semantics. Every input -- a valid field list, a
partially invalid one, and a wholly invalid one -- produces byte-identical
responses, because the deprecation short-circuits before any field handling.
No probe can learn anything about field selection from this surface, and the
required outcome is abstention rather than a belief inferred from a uniform
failure.
