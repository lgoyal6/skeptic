# Skeptic date evidence walkthrough

The page is generated from the committed Frankfurter v1 and v2 recordings and
uses the same deterministic check as `fixtures/checks.py`. It shows a Sunday
substitution, a Friday control that must abstain, and a later-version Sunday
that matches the requested date.

Regenerate the data bundle from the repository root:

```sh
python3 scripts/export_date_demo.py --source-commit "$(git rev-parse HEAD)"
```

The viewer makes no network or model calls. It checks the pinned hashes for the
fixture, rule, guard, and explanation files. It does not claim to verify rates
against a central-bank source or to describe the current live API.
