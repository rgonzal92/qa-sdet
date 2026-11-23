---
name: qa-plan
description: Test plan and test cases for a Jira item or feature description. Use when asked for a test plan, test cases or test coverage.
argument-hint: <JIRA-KEY | "feature description">
---

Write a test plan + test cases for: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. Read `${CLAUDE_SKILL_DIR}/../../references/common.md` steps C1 and C8.

1. C1 config.
2. **Requirement:** C8 for a Jira key; otherwise the given text.
3. **Memory:** `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem search '<2-3 key terms>'`, `... qa-mem card <route>` for pages the ticket touches, `... qa-mem bugs` and `... qa-mem bugs FIXED` for past bugs (error-guessing input).
4. **Analyse:** list ambiguous words, missing rules, roles and states as questions tagged BLOCKING / HIGH / LOW; AC hold only what the ticket states. Done when every AC and every question is numbered.
5. **Design:** read `${CLAUDE_SKILL_DIR}/../../references/test-design.md` and `${CLAUDE_SKILL_DIR}/../../references/plan-template.md`. Cover each AC with EP / BVA / decision table / state transition / negative as fits; mark cases better done at API/unit level; one case per distinct check, P1 first. Done when every AC has at least one case.
6. **Write** `${CLAUDE_PROJECT_DIR}/qa-out/<KEY>/plan.md` (from text: `qa-out/plan-<slug>/`, so it never shares a page's exploration folder) and `cases.md` (template), then `run.json` per `${CLAUDE_SKILL_DIR}/../../references/run-json.md`: mode `plan`, build `unknown`, only facts stated in the ticket (source = the key) under the routes the ticket touches; `"pages": []` if no route is known. No coverage, no bugs.
7. **Reply** ≤10 lines: paths, case count by priority, BLOCKING questions verbatim. Offer `/qa-sdet:qa-ticket <KEY>` to execute.

Cases go to Jira only through the user: print the acli command for them to run.
