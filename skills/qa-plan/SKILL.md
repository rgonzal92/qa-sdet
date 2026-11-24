---
name: qa-plan
description: Test plan and test cases for Jira items or a feature description, written as a readable report. Use when asked for a test plan, test cases or test coverage without running them (no browser); to run the tests, use qa-ticket.
argument-hint: <ticket numbers or keys, e.g. 1234 1250 | "feature description">
---

Write a test plan + test cases for: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. Read `${CLAUDE_SKILL_DIR}/../../references/common.md` (its Quoting rule, steps C1, C3, C7 and C8). `qa-mem` is the plugin's own command (its `bin/` folder is on the Bash PATH); only if the shell says `qa-mem: command not found`, use `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem` instead. This skill never opens a browser.

1. **C1 config.**
2. **Requirement:** for ticket numbers or keys, `qa-mem keys <args, each single-quoted>` (on exit 2 show its message; a missing prefix → ask, save, retry), then C8 for each key. Otherwise the given text is the requirement.
3. **Run folder:** `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --keys K1,K2 --prefix plan-` (from text: `--label '<2-4 word slug>' --prefix plan-`) → `<run>`.
4. **Memory:** `qa-mem search '<2-3 key terms>'`, `qa-mem code` (routes from source, if mapped) and `qa-mem code '<route>'` for the fields, rules and endpoints of pages the ticket touches (cite them in cases as "(from code)": they show what the code does, not what the ticket asks), `qa-mem card '<route>'`, `qa-mem bugs` and `qa-mem bugs FIXED` for past bugs (error-guessing input).
5. **Analyse:** list ambiguous words, missing rules, roles and states as questions tagged BLOCKING / HIGH / LOW; AC hold only what the ticket states. Done when every AC and every question is numbered.
6. **Design:** read `${CLAUDE_SKILL_DIR}/../../references/test-design.md`, `${CLAUDE_SKILL_DIR}/../../references/plan-template.md` and `${CLAUDE_SKILL_DIR}/../../references/report.md`. Cover each AC with EP / BVA / decision table / state transition / negative as fits; one case per distinct check, P1 first; checks better done at API/unit level go in `out_of_scope`. Done when every AC has at least one case with a route (empty when unknown).
7. **Write** `<run>/.data/plan.json` with kind `plan`: tickets (key, summary, AC, plan), cases and questions. From text: one ticket entry with key `FEATURE`. Then C7 `qa-mem report '<run>'`.
8. **Reply** ≤10 lines, in this order: case count by priority, BLOCKING questions verbatim; the `<run>/REPORT.md` path; then one next step. For a plan from tickets: `/qa-sdet:qa-ticket <numbers>` (it reuses this plan and its cases). A plan from text (key `FEATURE`) has no ticket to pass there: `/qa-sdet:qa-explore <route>` for its main page instead.

Cases go to Jira only through the user: if they ask for that, print the acli command for them to run; never run it.
