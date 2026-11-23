---
name: qa-memory
description: Query the QA app memory (pages, facts, locators, coverage, known bugs) or mark a bug filed or fixed.
argument-hint: doctor | browsers | card <route|name> | search <terms> | bugs [STATUS] | hash <route> <symptom> | bug-status <hash> <STATUS> | stale <build> | where
---

Run `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem <subcommand> '<arg>' ...` for: $ARGUMENTS. Single-quote every argument (search terms can come from ticket text), and show the output as-is. With no arguments, run `bugs` and `where`.

- The DB fills itself: the plugin hook ingests every `qa-out/**/run.json` after each run; `qa-mem` is its only interface.
- Manual writes: `bug-status <hash> FILED|FIXED|INVALID|OPEN` (after filing in Jira, add the key to the draft). A FIXED bug that is seen again turns REGRESSED by itself.
- `stale <build>` (manual write) after a deploy sets the current build and marks older knowledge for re-verification.
- `search` uses SQLite FTS5 syntax: `closure AND schedule`, `incident*`.
