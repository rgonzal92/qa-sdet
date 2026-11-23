---
name: qa-explore
description: Exploratory bug hunt on one page of the QA web app, no ticket needed, with evidence and repro specs. Use when asked to explore or find bugs on a page, URL or feature.
argument-hint: <url | route | page name> [--faults] [--budget N]
---

Explore and find bugs: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/common.md` (steps C1–C7).

1. C1 config, then resolve the target: full URL as given (its host must be in `allowedHosts`; if not, stop and say so: the guard would deny every page load); a route → `qaBaseUrl + route`; a page name → `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem card '<name>'` shows its route; if NO CARD, ask the user for the route. A route with `:id` needs a real record id: ask the user, or give the explorer the list page's route and ask it to open one record from there.
2. C2 login check.
3. C3: `out=${CLAUDE_PROJECT_DIR}/qa-out/<slug>`.
4. C4 run marker.
5. C5 card for the route.
6. C6 delegate with mode `explore`.
7. When it returns: `rm -f ${CLAUDE_PROJECT_DIR}/qa-out/.active` and the snapshot prune (C4 end). Relay its summary as-is (≤20 lines) with paths. Offer one next step: re-run with `--faults`, or, for a CONFIRMED bug, show the user the acli command at the bottom of its draft to run themselves (Jira writes are theirs; after filing, `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem bug-status <hash> FILED`).
