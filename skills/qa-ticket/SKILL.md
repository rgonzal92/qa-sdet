---
name: qa-ticket
description: Test one or more Jira items in the QA app: test plan, test cases, a run per page, and a report with a verdict per ticket. Use when given Jira keys or ticket numbers to test or QA ("test STUDIO-123", "QA 1234"). For a plan or cases without running them, use qa-plan.
argument-hint: <ticket numbers or keys, e.g. 1234 1250 OPS-7> [--faults] [--budget N] [--minutes M] [--review]
---

Test these Jira items and report a verdict per ticket: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/common.md` (steps C0–C8). `qa-mem` is the plugin's own command (its `bin/` folder is on the Bash PATH); only if the shell says `qa-mem: command not found`, use `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem` instead.

Flags: `--faults`, `--budget N` and `--minutes M` (both optional, per route; without them the run has no limit), `--review` (stop after the test cases). Every other argument is a ticket.

## 0. Set up
1. C1 config (asks for `jiraKeyPrefix` once when missing).
2. `qa-mem keys <ticket args, each single-quoted>` prints the full keys as a JSON list (`1234` → `STUDIO-1234`). On exit 2, show its message and stop (or, for a missing prefix, ask for it, save it, and retry).
3. `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --keys K1,K2,…` → `<run>`.

## 1. Analyze
- C8 for each key: summary, description, AC numbered AC-1..n per ticket.
- Known Jira bugs (read-only, via the C8 path; skip with `jira=paste`): with acli `acli jira workitem search --jql 'project = <jiraProject> AND type = Bug AND statusCategory != Done AND text ~ "<2-3 key terms>"' --limit 10`; the JQL goes in single quotes, and terms are plain words (letters, digits, spaces); with a Jira MCP, its JQL search tool with the same JQL and limit 10. Keep keys + summaries only.
- Memory: `qa-mem code` (routes and page names from source, after `/qa-sdet:qa-init`), `qa-mem code '<route>'` (a page's fields, rules, endpoints), `qa-mem search '<terms>'`, `qa-mem bugs`.

Done when every ticket has its numbered AC list and every AC has a route: the page where its check starts (an AC that continues on another page stays with the starting page; the explorer follows it there). Ask the user for routes you can't place. A route with `:id` needs a real record id: ask the user, or use the list page's route and let the explorer open a record.

## 2. Plan
Plan reuse: take the newest plan.json with exactly these keys in `tickets[].key` from either `${CLAUDE_PROJECT_DIR}/qa-out/*-plan-*/.data/plan.json` (from `/qa-sdet:qa-plan`) or an earlier `--review` run's `${CLAUDE_PROJECT_DIR}/qa-out/*/.data/plan.json` (kind `ticket`, no `run-*.json` next to it, so the user's edits are kept): write `<run>/.data/plan.json` with kind `ticket`, its title, tickets, cases and questions, a new `created` and this run's env; a case with an empty `route` (qa-plan leaves unknown ones empty) gets its AC's route from step 1. Then C7 `qa-mem report '<run>'`. With `--review`, stop as in step 3; otherwise go to step 4.
Otherwise read `${CLAUDE_SKILL_DIR}/../../references/report.md` (plan.json schema), `${CLAUDE_SKILL_DIR}/../../references/plan-template.md` (what goes in each field) and `${CLAUDE_SKILL_DIR}/../../references/test-design.md`. Write each ticket's AC and plan (scope, approach, risks, out of scope, test data) per ticket.

## 3. Test cases
Write cases TC-1..n (unique across tickets), each with its ticket, AC, title, route, priority, technique, steps and expected result, as plan-template.md says (one input value per case, no conditional steps, expected from the AC never from code). Every AC gets at least one case, P1 first, with its boundary and negative cases from the AC and the code map's rules for its route. Checks that only an API or unit test can prove, and failures this run can't cause, go in the plan's `out_of_scope`; with `--faults`, an AC's API error and empty-response cases are cases instead.
Write `<run>/.data/plan.json`, then C7 `qa-mem report '<run>'` (every case shows "not run").
With `--review`: stop here. Reply with the case count per ticket, the REPORT.md path, and one next step: reply `go` to run the cases (they may edit `<run>/.data/plan.json` first). Wait. On `go`, Read `<run>/.data/plan.json` again (it may have changed; keep every id the user left, and give a case they added with a missing or repeated id the next free TC number, saving the file), run C7 `qa-mem report '<run>'`, and test the cases it now holds in step 4.

## 4. Test
C0 browser tool, then C2 login check. Group the cases by route. For each route, one at a time:
1. C4 `marker start` with the user's flags.
2. C5 card for the route.
3. C6 with mode `ticket`, `slug=<route slug>`, target `qaBaseUrl + route`, this route's cases, all AC of the tickets these cases belong to, and the known Jira bugs. Ask it to run the cases first, then explore around them. When the route is a list page standing in for a `:id` page (step 1), also tell it to open a record.
4. C4 `marker stop`, then C7 `qa-mem report '<run>'`.

If an explorer returns BLOCKED (after C0's one retry for `browser tool failed`), skip the remaining routes (their cases stay NOT_RUN) and keep its summary: it names the fix (`/qa-sdet:qa-login`, or signing in to or installing the MCP's browser).

## 5. Track
Each explorer records a status per case (PASS, FAIL, BLOCKED, NOT_RUN, QUESTION) with the actual result, notes and bug id in its `run-<slug>.json`; the report picks them up. Done when every route has run or was skipped as above.

## 6. Report
C7 `qa-mem report '<run>'` once more, then C4 `prune`.

## 7. Verdict
The renderer decides each ticket's verdict (✅ PASS, ⚠️ PASS WITH NOTES, ❌ FAIL, ⛔ BLOCKED, ⏳ NOT STARTED) by the rules in `report.md`; report it as rendered, never your own. Reply in ≤12 lines, in this order: the Summary table from `<run>/REPORT.md` as rendered; the REPORT.md path (and, if REPORT.md has a `Problems reading this folder` section, say so: some results are missing); then one next step: a BLOCKED explorer's fix if any, otherwise for each CONFIRMED draft in `<run>/bugs/` the C8 filing command for the user to run (Jira writes are theirs; after filing, `qa-mem bug-status <hash> FILED`), otherwise none.
