---
name: qa-ticket
description: Test one or more Jira items in the QA app: test plan, test cases, a run per page, and a report with a verdict per ticket. Supports resuming interrupted runs (--resume) and selective retesting after fixes (--retest). Use when given Jira keys or ticket numbers to test, resume or retest ("test PROJ-123", "QA 1234", "retest 1234"). For a plan or cases without running them, use qa-plan.
argument-hint: <ticket numbers or keys, e.g. 1234 1250 OPS-7> [--faults] [--budget N] [--minutes M] [--review] [--resume [run]] [--retest [run]] [--all] [--verify]
---

Test these Jira items and report a verdict per ticket: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/run-setup.md` and `${CLAUDE_SKILL_DIR}/../../references/jira.md`.

Flags: `--faults`, `--budget N` and `--minutes M` (optional, per route), `--review` (stop after the test cases), `--resume [run]` (finish unfinished `NOT_RUN`/`BLOCKED` routes of an interrupted run in place), `--retest [run]` (new run folder reusing the prior run's `plan.json` and TC IDs, running only non-`PASS` cases and passes the prior verdict didn't trust unless `--all` is given). Every other argument is a ticket.

## 1. Analyze
1. Config (run-setup; ask for `jiraKeyPrefix` once when missing).
2. If `--resume <run>` or `--retest <run>` names a run folder with no ticket keys, run `qa-mem plan-find '${CLAUDE_PROJECT_DIR}' --run '<run>'` to get its ticket keys; otherwise run `qa-mem prep '${CLAUDE_PROJECT_DIR}' <ticket args, each single-quoted>` (exit 2: show its message and stop; missing `jiraKeyPrefix`: ask, save, retry). `qa-mem prep` returns expanded keys, config, Jira tickets (with `jiraAcField`) + comments (or `acli: false` -> fall back to pasted text per `jira.md`), `plan_state` (`reusable_plan` + `latest_run`), `rules` (`qa-rules.md`), code-map routes, and open/fixed bugs in one call.
3. For touched routes, run `qa-mem code '<route>'`, `qa-mem search '<terms>'`, and the read-only Known bugs Jira search (`jira.md`).
4. When not `--review`, run the **Pre-flight session check** (run-setup; pass `--build` on `--resume` to detect build changes).

Done when every ticket has numbered AC and every AC has a route: the page where its check starts (an AC that continues on another page stays with the starting page). Ask the user for routes you can't place.

## 2. Plan and cases
- **`--resume [run]`:** use the folder from `--run` or `plan_state.latest_run`. Stop if missing, `can_resume` is false, or the live build from `session-check --build` differs from `builds` (tell the user to use `--retest` on a new build). Keep `<run>` and its `plan.json` as-is; Step 3 runs only `incomplete_routes`, overwriting any `BLOCKED`/`INCOMPLETE` `run-<slug>.json` while preserving completed routes.
- **`--retest [run] [--all]`:** use the folder from `--run` or `plan_state.latest_run` (stop if missing or `can_retest` is false). Create a fresh run folder via `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --keys K1,K2`, read the prior `plan.json`, keep every `TC-n` id verbatim, refresh any resolved questions from the new Jira comments, and write `<run>/.data/plan.json` with `"retest_of": "<prior run folder name>"`. Without `--all`, move `passed_cases` into `"prior_passed"` (keeping each case's prior `build`) and keep in `"cases"` exactly the prior plan's case objects (steps and expected included) whose ids are in `retest_cases` (`FAIL`/`BLOCKED`/`NOT_RUN`/`QUESTION`, plus `PASS` cases from a run that ended `BLOCKED` or on the page of a CONFIRMED CRITICAL/HIGH bug no retested case names: keep them; `retest_cases` itself only names them); with `--all`, keep every case in `"cases"`.
- **Normal reuse:** when `plan_state.reusable_plan` exists (a `qa-plan` run, a `--review` run, or a `BLOCKED` run where zero cases executed), create `<run>` via `qa-mem rundir` and write `<run>/.data/plan.json` from it (kind `ticket`, new `created` and env; fill empty `route`s from Step 1).
- **Otherwise:** create `<run>` via `qa-mem rundir`, read `${CLAUDE_SKILL_DIR}/../../references/planning.md`, and write each ticket's plan and cases `TC-1..n` (unique across tickets; honor `rules` from `qa-rules.md` so forbidden actions are `out_of_scope` rather than `BLOCKED` cases; populate `fixed_note` when clear from the ticket). Every AC gets a case, P1 first, with boundary and negative cases from the AC and code-map rules. With `--faults`, an AC's API error and empty-response cases are cases, not out of scope.

Run `qa-mem report '<run>'`.
With `--review`, stop: reply with the case count per ticket, the REPORT.md path and one next step: reply `go` to run the cases (they may edit `<run>/.data/plan.json` first). On `go`, run pre-flight `qa-mem session-check`, Read `plan.json` again (keep every id the user left; a case they added with a missing or repeated id gets the next free TC number, saved to the file), run `qa-mem report '<run>'`, and test the cases it now holds.

## 3. Test
Group the cases to run by route (`incomplete_routes` only on `--resume`). For each route, one at a time, as run-setup says: `qa-mem dispatch '<run>' --route '<route>' --known-bugs '<the Known bugs search's keys and summaries>'` (run-setup's Setup; it puts this route's cases, all AC of their tickets and the known bugs in the brief, cases first), delegate with its brief, teardown.
If an explorer returns BLOCKED, skip the remaining routes (their cases stay NOT_RUN) and keep its summary (the user can continue later with `/qa-sdet:qa-ticket --resume`).

With `--verify`: after the last teardown, run-setup's Second opinion (a `qa-sdet:qa-reviewer` read-only check of the serious drafts; its reply saved as `<run>/.data/review.json`).

## 4. Reply
The renderer decides each ticket's verdict (README, "Reading REPORT.md"): report it as rendered, never your own. Reply as run-setup's Reply says, but start with the verdict block at the top of `<run>/REPORT.md` as rendered (the Summary table when there are 2+ tickets). The one next step, in order: a BLOCKED explorer's fix (and `/qa-sdet:qa-ticket --resume` to continue); for a PASS or PASS WITH NOTES ticket the draft `<run>/jira-comment-<KEY>.md` (the user pastes it into Jira after checking its CHECK block: build, partial-retest note, images, placeholders); for each CONFIRMED draft the filing command; else none.
