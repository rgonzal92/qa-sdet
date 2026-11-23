# Test plan: <KEY> <title>

**Build/env:** <build> on QA · **Author:** qa-sdet draft · **Date:** <date>

## Acceptance criteria (as the ticket states them; qa-ticket reuses this list)
- AC-1: …
- AC-2: …

## Scope
<what is tested, 3–6 bullets, mapped to AC IDs>

## Out of scope
<explicitly not tested and why>

## Risks
| Risk | Impact | Likelihood | Covered by |
|---|---|---|---|

## Environment & data
<roles/accounts, seed data, feature flags, dependencies>

## Open questions
- Q1 [BLOCKING] …
- Q2 [HIGH] …
- Q3 [LOW] …

## Entry / exit
Entry: build deployed to QA, data available. Exit: all P1 cases run, no open HIGH+ bugs, questions answered or accepted.

--- (everything below goes in cases.md)

# Test cases: <KEY>

| ID | Title | AC | Preconditions | Steps | Expected | Priority | Technique | Level |
|---|---|---|---|---|---|---|---|---|
| TC-01 | | AC-1 | | 1. … 2. … | | P1 | BVA | UI / API / unit |
