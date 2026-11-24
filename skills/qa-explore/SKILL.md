---
name: qa-explore
description: Exploratory bug hunt on one page of the QA web app, no ticket needed, with annotated bug images, repro specs and a readable report. Use when asked to explore or find bugs on a page, URL or feature.
argument-hint: <url | route | page name> [--faults] [--budget N] [--minutes M]
---

Explore and find bugs: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/run-setup.md`. `--faults`, `--budget N` and `--minutes M` are optional and go to the setup call; without them there is no fault injection and no limit.

1. Config, then the target: a full URL as given (its host must be in `allowedHosts`; if not, stop and say so: the guard would deny every page load); a route is `qaBaseUrl + route`; a page name: `qa-mem card '<name>'` shows its route (pages mapped by `/qa-sdet:qa-init` count too; `qa-mem code` lists them). NO CARD: ask the user for the route and pass on the card's `tip:` line in your reply.
2. Setup call (run-setup), then write `<run>/.data/plan.json`: `{"kind": "explore", "title": "<route>", "created": "<date -u +%Y-%m-%dT%H:%M:%SZ>", "env": {"url": "<target>", "browser": "<config browser>"}, "tickets": [], "cases": [], "questions": []}`. When the user listed checks to run, they are `cases` (fields in `${CLAUDE_SKILL_DIR}/../../references/planning.md`).
3. Delegate with mode `explore` and `slug=<route slug>`. With cases in plan.json, pass them (id, steps, expected) and ask the explorer to run them first.
4. Teardown, then the Reply (run-setup).
