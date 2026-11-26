---
name: qa-ticket
description: Test one or more Jira items in the QA app: test plan, test cases, a run per page, and a report with a verdict per ticket. Each ticket has its own folder, updated by every run: an interrupted run is resumed (--resume) and a fix retested (--retest) in place. Use when given Jira keys or ticket numbers to test, resume or retest ("test PROJ-123", "QA 1234", "retest 1234"). For a plan or cases without running them, use qa-plan.
argument-hint: <ticket numbers or keys, e.g. 1234 1250 OPS-7> [--faults] [--api-writes] [--review] [--resume] [--retest] [--all] [--verify]
---

Test these Jira items and report a verdict per ticket: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/run-setup.md` and `${CLAUDE_SKILL_DIR}/../../references/jira.md`.

Flags: `--faults`, `--api-writes` (lets the explorer send POST, PUT, PATCH and DELETE with `qa-mem api-probe`, only on `qa-sdet-` records it created; reads always work), `--review` (stop after the test cases), and for a ticket tested before: `--resume` (the cases still open), `--retest` (the cases that did not pass, and the passes the last verdict did not trust), `--all` (every case again). Every other argument is a ticket.

**One ticket at a time.** A ticket's folder `<run>` is `qa-out/<KEY>/`: its plan, cases, bugs, evidence and report, kept and updated by every run of that ticket. Do steps 1 to 3 for the first ticket, then for the next one; step 4 once, at the end. An argument that is a folder under `qa-out/` instead of a ticket: `qa-mem plan-find '${CLAUDE_PROJECT_DIR}' --run '<folder>'` gives its ticket keys.

## 1. Analyze
1. Config (run-setup; ask for `jiraKeyPrefix` once when missing).
2. `qa-mem prep '${CLAUDE_PROJECT_DIR}' '<ticket>'` (exit 2: show its message and stop; missing `jiraKeyPrefix`: ask, save, retry). It returns the expanded key, config, the Jira ticket (with `jiraAcField`) + comments (or `acli: false` -> fall back to pasted text per `jira.md`), `plan_state` (`reusable_plan` + `latest_run`), `rules` (`qa-rules.md`), code-map routes, and open/fixed bugs in one call.
3. Start from prep's `context`: `candidate_routes` (the routes the tickets' words point at, with why), `impacted_routes` (pages those may reach: sibling writers, readers of what they write, shared components; a regression case or a lead for each that matters), `relevant_facts` and `relevant_bugs` (status kept). For the routes the tickets touch, run `qa-mem code '<route>'` and `qa-mem search '<terms>'` (stemmed: `date` finds `dates`; it searches facts, bugs and the code map), and the read-only Known bugs Jira search (`jira.md`).
4. When not `--review`, run the **Pre-flight session check** with `--build` (run-setup): the build it prints picks the row in step 2.

Done when the ticket has numbered AC and every AC has a route: the page where its check starts (an AC that continues on another page stays with the starting page). Ask the user for routes you can't place.

## 2. Plan and cases
`<run>` = `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --keys <KEY>`. Take the first of these that fits `plan_state`:
- **Tested here before** (`latest_run.run_dir` is `<run>` and `latest_run.has_runs`): keep `<run>/.data/plan.json`. When `plan_state.drift` names the ticket, its Jira summary, description, AC field or fix versions changed since that plan: tell the user what changed and ask once whether to regenerate its ACs and cases. Yes: regenerate them, keeping the ids of cases that stay, and treat this run as `--all`. No: set the ticket's `drift` in `plan.json` (planning.md). Then the first row that fits says what step 3 runs:

  | When | Routes | Each dispatch gets |
  |---|---|---|
  | `--all`, or the live build is not in `latest_run.builds` | every route with a case | `--all` |
  | `--retest` | the routes of `latest_run.retest_cases` | `--cases <that route's ids in retest_cases>` |
  | `latest_run.incomplete_routes` is not empty (`--resume`, or no flag) | `incomplete_routes` | nothing more: it gives the open cases |
  | none of these | none | nothing runs: reply with the verdict `<run>/REPORT.md` shows, say the ticket is already tested on this build, name `--retest` and `--all`, and go on with the next ticket |

  The tool keeps the result of every case a run does not get, with the build it ran on.
- **A plan waits** (`reusable_plan`: from `qa-plan`, a `--review`, or a run where no case executed): make it `<run>/.data/plan.json` (kind `ticket`, new `created` and env; fill empty `route`s from step 1).
- **Tested only in an older dated folder** (`latest_run.run_dir` is not `<run>`; `subset_of`: this ticket's part of a combined run) and `--retest`: planning.md, "Retest from an older folder".
- **Otherwise:** read `${CLAUDE_SKILL_DIR}/../../references/planning.md` and write the ticket's plan (with its `source_snapshot` from prep) and cases `TC-1..n` (honor `rules` from `qa-rules.md` so forbidden actions are `out_of_scope` rather than `BLOCKED` cases; populate `fixed_note` when clear from the ticket). Every AC gets a case, P1 first, with boundary and negative cases from the AC and code-map rules. With `--faults`, an AC's API error and empty-response cases are cases, not out of scope.

Run `qa-mem report '<run>'`.
With `--review`: do steps 1 and 2 for every ticket, then stop: reply with the case count per ticket, each REPORT.md path and one next step: reply `go` to run the cases (they may edit a `<run>/.data/plan.json` first). On `go`, run pre-flight `qa-mem session-check --build`, and per ticket Read its `plan.json` again (keep every id the user left; a case they added with a missing or repeated id gets the next free TC number, saved to the file), run `qa-mem report '<run>'`, and test the cases it now holds.

## 3. Test
For each route step 2 named (a new plan: every route with a case), one at a time, as run-setup says: `qa-mem dispatch '<run>' --route '<route>' --known-bugs '<the Known bugs search's keys and summaries>'` plus the row's flag (run-setup's Setup; it puts the route's cases, the ticket's AC and the known bugs in the brief, cases first), delegate with its brief, teardown.
After each route's teardown, `qa-mem next-action '<run>' <slug>` says what comes next (run-setup's After each explorer): `continue` (a page-local blocker stays that route's BLOCKED; the other routes go on), `recover_sso` (sign in again, once per invocation, then that route again) or `abort` (skip the remaining routes and tickets, their cases stay NOT_RUN; keep its reason; the user continues later with the same command).

With `--verify`: after a ticket's last teardown, run-setup's Second opinion (a `qa-sdet:qa-reviewer` read-only check of the serious drafts; its reply saved as `<run>/.data/review.json`).

## 4. Reply
The renderer decides each ticket's verdict (README, "Reading REPORT.md"): report it as rendered, never your own. Reply as run-setup's Reply says, but start with one line per ticket: its verdict as the top of its `<run>/REPORT.md` renders it, and that file's path. The one next step, in order: a BLOCKED explorer's fix (and the same command again to continue); for each ticket its `<run>/jira-comment.txt` (the result per acceptance criterion and its bugs, for any verdict) and the `acli jira workitem comment create` command REPORT.md's What to do shows (the user runs it after the checks listed under it; never run it); for each CONFIRMED draft the filing command (or, for a bug already in Jira, the comment command); else none.
