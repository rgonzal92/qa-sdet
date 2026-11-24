---
name: qa-explore
description: Exploratory bug hunt on one page of the QA web app, no ticket needed, with annotated bug images, repro specs and a readable report. Use when asked to explore or find bugs on a page, URL or feature.
argument-hint: <url | route | page name> [--faults] [--budget N] [--minutes M]
---

Explore and find bugs: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/common.md` (steps C0–C7, and C8's filing command). `qa-mem` is the plugin's own command (its `bin/` folder is on the Bash PATH); only if the shell says `qa-mem: command not found`, use `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem` instead.

`--budget N` and `--minutes M` are optional; without them the run has no limit.

1. C1 config, then resolve the target: full URL as given (its host must be in `allowedHosts`; if not, stop and say so: the guard would deny every page load); a route → `qaBaseUrl + route`; a page name → `qa-mem card '<name>'` shows its route (pages mapped from source by `/qa-sdet:qa-init` count too; `qa-mem code` lists them); if NO CARD, ask the user for the route, and pass on the card's `tip:` line in your reply. A route with `:id` needs a real record id: ask the user, or give the explorer the list page's route and ask it to open one record from there.
2. C0 browser tool, then C2 login check.
3. C3: `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --route '<route>'` → `<run>`. Write `<run>/.data/plan.json` with kind `explore` (schema in `${CLAUDE_SKILL_DIR}/../../references/report.md`): title = the route, env, no tickets, and cases only when the user listed checks to run.
4. C4 `marker start` with the user's flags.
5. C5 card for the route.
6. C6 delegate with mode `explore` and `slug=<route slug>`. When plan.json has cases, pass them (id, steps, expected) as in ticket mode and ask the explorer to run them first.
7. When it returns: C4 `marker stop`, C7 `qa-mem report '<run>'`, C4 `prune`. Reply ≤12 lines, in this order: the explorer's status (COMPLETED, INCOMPLETE or BLOCKED), bugs `[SEV] title`, top untested risks; the `<run>/REPORT.md` path (and, if REPORT.md has a `Problems reading this folder` section, say so: some results are missing); then one next step: a BLOCKED explorer's fix if any, otherwise for each CONFIRMED draft in `<run>/bugs/` the C8 filing command for the user to run (Jira writes are theirs; after filing, `qa-mem bug-status <hash> FILED`), otherwise a re-run with `--faults` (when this run had none).
