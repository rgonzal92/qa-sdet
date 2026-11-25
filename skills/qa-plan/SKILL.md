---
name: qa-plan
description: Test plan and test cases for Jira items or a feature description, written as a readable report. Use when asked for a test plan, test cases or test coverage without running them (no browser); to run the tests, use qa-ticket.
argument-hint: <ticket numbers or keys, e.g. 1234 1250 | "feature description">
---

Write a test plan + test cases for: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. Read `${CLAUDE_SKILL_DIR}/../../references/planning.md`, and `jira.md` there when given ticket keys. This skill never opens a browser. Every user, ticket or page text in a shell command goes in single quotes, each `'` inside written `'\''`.

1. **Requirement:** for ticket numbers or keys: read `~/.config/qa-sdet/config.json` (missing: tell the user to run `/qa-sdet:qa-setup` and stop), then `qa-mem prep '${CLAUDE_PROJECT_DIR}' <args, each single-quoted>` (exit 2: show its message; a missing `jiraKeyPrefix`: ask, save it in the config, retry). It returns expanded keys, Jira tickets (with `jiraAcField`) + comments, `qa-rules.md`, routes, and open/fixed bugs in one call (if `acli` is false, fall back to pasted text per `jira.md`). Otherwise the given text is the requirement (still check `<project>/qa-rules.md` if present).
2. **Run folder:** `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --keys K1,K2 --prefix plan-` (from text: `--label '<2-4 word slug>' --prefix plan-`) gives `<run>`.
3. **Memory:** for ticket keys, prep's `context` first (`decisions`: questions answered before, oracles you don't ask again; `patterns`: how this app broke on 2+ pages before, worth a negative case; `candidate_routes` with why, `relevant_facts`, `relevant_bugs`); then `qa-mem search '<2-3 key terms>'` (stemmed; facts, bugs and the code map), `qa-mem code '<route>'` and `qa-mem card '<route>' --focus '<the ticket's subject>'` for the pages the ticket touches (plus `qa-mem code`, `qa-mem bugs`, and `qa-mem bugs FIXED` when planning from text).
4. **Analyse:** list ambiguous words, missing rules, roles and states as questions tagged BLOCKING, HIGH or LOW (several tickets: each names its `ticket`, planning.md); AC hold only what the ticket states. Done when every AC and question is numbered.
5. **Design and write** `<run>/.data/plan.json` (kind `plan`; planning.md): honor `qa-rules.md` so forbidden actions go in `out_of_scope` instead of cases; cover each AC with the technique that fits, one case per distinct check, P1 first, every case with a route (empty when unknown). From text: one ticket entry with key `FEATURE`. Then `qa-mem report '<run>'`.
6. **Reply** (at most 10 lines), in this order: case count by priority and the BLOCKING questions verbatim; the `<run>/REPORT.md` path; then one next step. A plan from tickets: `/qa-sdet:qa-ticket <numbers>` (it reuses this plan and its cases). A plan from text has no ticket to pass there: `/qa-sdet:qa-explore <route>` for its main page.

Cases go to Jira only through the user: if they ask, print the acli command for them to run; never run it.
