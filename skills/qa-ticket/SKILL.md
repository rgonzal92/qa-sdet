---
name: qa-ticket
description: Test a Jira item's acceptance criteria in the QA app and hunt bugs around them. Use when given a Jira key to test or QA.
argument-hint: <JIRA-KEY> [--faults] [--budget N]
---

Test Jira item against its AC and find bugs: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/common.md` (steps C0–C8).

1. C1 config.
2. **AC:** reuse the AC and P1 cases from `${CLAUDE_PROJECT_DIR}/qa-out/<KEY>/plan.md` when it exists; otherwise C8.
3. **Routes:** which page(s) the AC touch (ticket text, `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem search '<terms>'`, or ask). Max 3. Map every AC to exactly one route: the page where its check starts (an AC that continues on another page, e.g. create on the form, then see it in the table, stays with the starting page; the explorer follows it there). Target URL = `qaBaseUrl + route`; a route with `:id` needs a real record id: ask the user, or pass the list page's route and let the explorer pick a record.
4. C0 browser tool, then C2 login check.
5. **Known Jira bugs (read-only, via the C8 Jira path; skip with `jira=paste`):** with acli `acli jira workitem search --jql "project = <jiraProject> AND type = Bug AND statusCategory != Done AND text ~ \"<2-3 key terms>\"" --limit 10`; terms are plain words (letters, digits, spaces), since they sit inside double quotes; with a Jira MCP, its JQL search tool with the same JQL and limit 10; keep keys + summaries only.
6. **Per route, one at a time:** C3 with `out=${CLAUDE_PROJECT_DIR}/qa-out/<KEY>/<slug>`, C4 run marker (fresh for each route; `--budget N` is per route), C5 card, C6 delegate with mode `ticket`, adding only this route's numbered AC (+ their P1 cases) and the known Jira bugs (keys + summaries), and asking it to test those AC first, then explore around them, reporting each as PASS / FAIL / NOT_RUN / QUESTION. When it returns: `rm -f ${CLAUDE_PROJECT_DIR}/qa-out/.active`. If it returned BLOCKED (after C0's one retry for `browser tool failed`), skip the remaining routes (their AC are NOT_RUN) and relay its summary: it names the fix (`/qa-sdet:qa-login`, or signing in to or installing the MCP's browser).
7. The snapshot prune (C4 end). Reply ≤15 lines: AC table (every AC-n → its route's status; an AC seen on several routes takes the worst: FAIL > QUESTION > NOT_RUN > PASS), bugs `[SEV] title → path`, questions, not tested.
