---
name: qa-ticket
description: Test one or more Jira items in the QA app: test plan, test cases, a run per page, and a report with a verdict per ticket. Use when given Jira keys or ticket numbers to test or QA ("test PROJ-123", "QA 1234"). For a plan or cases without running them, use qa-plan.
argument-hint: <ticket numbers or keys, e.g. 1234 1250 OPS-7> [--faults] [--budget N] [--minutes M] [--review]
---

Test these Jira items and report a verdict per ticket: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/run-setup.md` and `${CLAUDE_SKILL_DIR}/../../references/jira.md`.

Flags: `--faults`, `--budget N` and `--minutes M` (optional, per route), `--review` (stop after the test cases). Every other argument is a ticket.

## 1. Analyze
1. Config (run-setup; ask for `jiraKeyPrefix` once when missing).
2. `qa-mem keys <ticket args, each single-quoted>` prints the full keys as a JSON list (`1234` becomes `PROJ-1234`). Exit 2: show its message and stop (for a missing prefix: ask, save, retry). Then `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --keys K1,K2` gives `<run>`.
3. Per key (jira.md): summary, description, comments, AC-1..n, and the known Jira bugs.
4. Memory: `qa-mem code` (routes from source, after `/qa-sdet:qa-init`), `qa-mem code '<route>'` (a page's fields, rules, endpoints), `qa-mem search '<terms>'`, `qa-mem bugs`.

Done when every ticket has numbered AC and every AC has a route: the page where its check starts (an AC that continues on another page stays with the starting page). Ask the user for routes you can't place.

## 2. Plan and cases
**Reuse:** take the newest plan.json whose `tickets[].key` are exactly these keys, from `${CLAUDE_PROJECT_DIR}/qa-out/*-plan-*/.data/plan.json` (from `/qa-sdet:qa-plan`) or an earlier `--review` run's `${CLAUDE_PROJECT_DIR}/qa-out/*/.data/plan.json` (kind `ticket`, no `run-*.json` beside it, so the user's edits are kept). Write `<run>/.data/plan.json` with kind `ticket`, its title, tickets, cases and questions, a new `created` and this run's env; a case with an empty `route` gets its AC's route from step 1.
**Otherwise** read `${CLAUDE_SKILL_DIR}/../../references/planning.md` and write each ticket's plan and cases TC-1..n (unique across tickets). Every AC gets a case, P1 first, with boundary and negative cases from the AC and the code map's rules. With `--faults`, an AC's API error and empty-response cases are cases, not out of scope.
Then `qa-mem report '<run>'` (every case shows "not run").
With `--review`, stop: reply with the case count per ticket, the REPORT.md path and one next step: reply `go` to run the cases (they may edit `<run>/.data/plan.json` first). On `go`, Read plan.json again (keep every id the user left; a case they added with a missing or repeated id gets the next free TC number, saved to the file), run `qa-mem report '<run>'`, and test the cases it now holds.

## 3. Test
Group the cases by route. For each route, one at a time, as run-setup says: setup call (no `rundir`), delegate with mode `ticket`, `slug=<route slug>`, target `qaBaseUrl + route`, this route's cases, all AC of their tickets and the known Jira bugs (tell it to run the cases first, then explore around them), teardown.
If an explorer returns BLOCKED, skip the remaining routes (their cases stay NOT_RUN) and keep its summary.

## 4. Reply
The renderer decides each ticket's verdict (README, "Reading REPORT.md"): report it as rendered, never your own. Reply as run-setup's Reply says, but start with the verdict block at the top of `<run>/REPORT.md` as rendered (the Summary table when there are 2+ tickets). The one next step, in order: a BLOCKED explorer's fix; for a PASS or PASS WITH NOTES ticket the draft `<run>/jira-comment-<KEY>.md` (the user pastes it into Jira after checking its CHECK block: build, images, placeholders); for each CONFIRMED draft the filing command; else none.
