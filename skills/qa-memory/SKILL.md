---
name: qa-memory
description: Query the QA app memory (pages, facts, locators, coverage, known bugs) or mark a bug filed or fixed. Use when asked what qa-sdet knows about a page, which pages most need exploring, whether a bug is already known, to mark a bug filed or fixed, or to set the current build after a deploy. Mapping the app from source is qa-init.
argument-hint: doctor | browsers | card <route|name> | gaps | search <terms> | bugs [STATUS] | hash <route> <symptom> | bug-status <hash> <STATUS> | stale <build> | where
---

Run `qa-mem <subcommand> '<arg>' ...` for: $ARGUMENTS. Single-quote every argument, writing each `'` inside it as `'\''` (search terms can come from ticket text; a bare `'` would end the quoting and run the rest as a command), and show the output as-is. With no arguments, run `bugs` and `where`.

- The DB fills itself: the plugin hook ingests each run's `qa-out/<run>/.data/run-*.json` when an explorer finishes; `qa-mem` is its only interface.
- Manual writes: `bug-status <hash> FILED|FIXED|INVALID|OPEN` (after filing in Jira, add the key to the draft). A FIXED bug that is seen again turns REGRESSED by itself.
- `gaps` lists every page, the most urgent to explore first: never explored, source changed since its last visit (git, per the code map), verified on an older build, then the most untried heuristics and untested fields, then open bugs.
- `stale <build>` (manual write) after a deploy sets the current build and marks older knowledge for re-verification.
- `search` takes plain words, all of which must match (`closure schedule`); `-word` leaves out facts with that word. Whole words only: `incidents`, not `incident*`.
