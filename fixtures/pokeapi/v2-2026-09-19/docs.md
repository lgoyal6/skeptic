# PokeAPI v2, documentation snapshot

Source: https://pokeapi.co/docs/v2
Captured: 2026-09-19
Authentication: none. A Fair Use Policy asks callers to cache locally, which
is precisely what this fixture does.

## Documented promises exercised by this capture

- Resource lists are paginated with `limit` and `offset`.
- A list response carries `count` (the total available), `next` and `previous`
  (URLs, or null at the ends), and `results` (the page).

## Why this fixture is a control

`count` is a claim about the whole collection, and `results` is a page of it.
That pair is exactly the shape that lets a caller be misled -- Open Library
returns `numFound: 48168` with `docs: []` for `limit=0`, and the emptiness
looks like absence. PokeAPI is the same shape behaving correctly: asking for
more than exists returns everything, `count` equals the number returned, and
`next` is null because there is genuinely no next page. Pagination that
reports its own end accurately is the control against which the others are
mismatches.
