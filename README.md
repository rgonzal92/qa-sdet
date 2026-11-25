# qa-sdet

A Claude Code plugin for SDET work: it reads your Jira tickets, writes a test plan and test cases, runs them in the QA app with `playwright-cli`, hunts bugs around them, and renders one readable report per run with a verdict per ticket. A local SQLite memory of the app updates itself after every run.

## Install (per machine)

**Needs:** Linux, Claude Code, Python 3.9+ as `python3` (hooks run it), and `playwright-cli` (`npm i -g @playwright/cli`).
**Optional:** `acli` signed in (`acli jira auth login`) for Jira reads, `git` for repo-aware code scans and `.git/info/exclude`, and Pillow for a text label beside the red box on evidence screenshots (without it, and inside iframes, `playwright-cli highlight` draws the box with no label; `qa-mem doctor` and `/qa-sdet:qa-setup` print the install command that works on your system: `pip install --user`, or `apt-get` where system Python blocks `pip`).

From GitHub (nothing to clone):
```sh
claude plugin marketplace add rgonzal92/qa-sdet
claude plugin install qa-sdet@qa-sdet
```

From a local clone (optional, any folder):
```sh
git clone https://github.com/rgonzal92/qa-sdet.git /any/folder/qa-sdet
claude plugin marketplace add /any/folder/qa-sdet     # absolute path
claude plugin install qa-sdet@qa-sdet
```

Inside Claude Code's `/plugin` command, `~` is not expanded: pass the full path (`/home/you/...`), or it is parsed as a bad `owner/repo`.

To update:
```sh
claude plugin marketplace update qa-sdet && claude plugin update qa-sdet@qa-sdet
```
Then restart Claude Code.

> **Upgrading from 0.3.0:** a pre-0.4 memory DB is refused with its file path. Move it aside; `qa-mem` creates a new DB and `/qa-sdet:qa-init` maps the code again.

## Quick start

1. `/qa-sdet:qa-setup` — checks tools, offers the Pillow install, sets your Jira ticket prefix (e.g. `PROJ`), and adds auto-mode allow rules.
2. `/qa-sdet:qa-login` — sign in to the QA app once (SSO + MFA in a browser window).
3. `/qa-sdet:qa-inbox` — lists your open Jira tickets, marking NEW ones since last check.
4. `/qa-sdet:qa-ticket 1234 1250` — plans, generates cases, runs them, and writes `REPORT.md`.

## Commands

| Command | What it does |
|---|---|
| `/qa-sdet:qa-setup` | One-time setup: tool check, Pillow, ticket prefix, allow rules, old cookie file cleanup |
| `/qa-sdet:qa-login` | Sign in once in qa-sdet's own browser profile; later runs reuse it |
| `/qa-sdet:qa-inbox` | Open Jira tickets (default: assigned to you, or `inboxJql`), `NEW` / `CHANGED` first with local `Last QA` verdicts, plus read-only `bug-sync` for filed bugs; saved to `qa-out/INBOX.md` |
| `/qa-sdet:qa-ticket 1234 1250 [--faults] [--api-writes] [--budget N] [--minutes M] [--review] [--resume [run]] [--retest [run]] [--all]` | Tests tickets end to end with a verdict per ticket; `--resume` finishes unfinished routes in place, `--retest` re-runs failed/blocked/not-run cases and passes the prior verdict didn't trust (or `--all`) on the saved plan; `--api-writes` as for `qa-explore` |
| `/qa-sdet:qa-plan 1234` or `"text"` | Test plan + test cases only, no browser; `qa-ticket` reuses them |
| `/qa-sdet:qa-explore <url \| route \| page name>... \| --changed \| --top N \| --discover [<url>] [--faults] [--api-writes] [--budget N] [--minutes M] [--total-budget N] [--total-minutes M] [--focus '<charter>'] [--verify]` | Exploratory bug hunt on one or more pages (or `--changed` / `--top N` from `qa-mem gaps`) into one report; with no page, offers the top 5; `--focus` narrows to a charter; `--discover [<url>] [--depth D]` maps pages, in-page states and links read-only (no source code needed); `--continue [run]` picks up an explore run's unfinished routes and leads, `--adaptive` follows up to 2 of them in the same run when `explore-next` says it is worth it (`expand_recommended`), within the user's `--total-budget` / `--total-minutes` or, without them, a default ceiling of 120 browser commands and 20 minutes for the extra pages together; `--api-writes` lets the explorer send POST, PUT, PATCH and DELETE with `qa-mem api-probe`, only on its own `qa-sdet-` records (the mass-assignment probe; reads always work) |
| `/qa-sdet:qa-init [folders] [--full] [--crawl [--budget N] [--minutes M]]` | Maps routes, locators, validation rules and endpoints from source into memory; `--crawl` verifies locators live |
| `/qa-sdet:qa-memory <subcommand>` | Queries or updates memory (`card`, `gaps`, `search`, `bugs`, `bug-status`, `bug-sync`, `stale`, `hash`, `doctor`, `browsers`, `where`) |

- **Tickets:** bare numbers use `jiraKeyPrefix` (`1234` → `PROJ-1234`); full keys (`OPS-7`) work too; pass several separated by spaces or commas. Plain requests ("write test cases for 1234", "explore the closures page") also trigger the matching skill.
- **`qa-mem` CLI:** skills call `qa-mem` bare because the plugin's `bin/` directory is on the Bash `PATH` while enabled. Outside Claude Code (or if `qa-mem` is not on `PATH`), run `python3 <plugin folder>/bin/qa-mem`.
- **Optional limits:** runs continue until done unless you pass `--budget N` (browser commands) or `--minutes M` (up to 480). Limits apply per page in `qa-ticket`/`qa-explore` and to the whole crawl in `qa-init --crawl`; the guard hook enforces them. `qa-explore --total-budget N` / `--total-minutes M` cap the whole run across its pages: before each page the coordinator asks `qa-mem spend` and dispatches it with what is left (so the guard enforces the total too), and stops when less than 10 commands or 2 minutes remain.

### What `qa-ticket` does

1. **Analyze** each ticket: summary, comments, numbered acceptance criteria (AC), and related open Jira bugs.
2. **Plan** per ticket: scope, approach, risks, out of scope, and test data.
3. **Test cases** `TC-1..n`, each tied to an AC, with steps and expected results. The initial report is written now (all cases "not run"). `--review` stops here so you can inspect or edit `<run>/.data/plan.json`; reply `go`, or re-run `/qa-sdet:qa-ticket` without `--review` to reuse your edited plan.
4. **Test** with `playwright-cli`: one explorer per page, sequentially.
5. **Track** one status per case (`PASS`, `FAIL`, `BLOCKED`, `NOT_RUN`, or `QUESTION`), recording observed behavior, evidence image, and any bug found.
6. **Report:** `REPORT.md`, re-rendered after every page, with a verdict per ticket: ✅ PASS · ⚠️ PASS WITH NOTES · ❌ FAIL · ⛔ BLOCKED · ⏳ NOT STARTED (rules below).

## Output

```
qa-out/
  README.md                       index of all runs, newest first: date, run, verdicts, bugs
  INBOX.md                        from qa-inbox
  guard-denials.log               guard and auto-mode denials concerning qa-sdet (secrets masked)
  init/                           from qa-init: scan-<area>.json and code-map-<area>.json
  2026-11-23-PROJ-1234+PROJ-1250/
    REPORT.md                     open this: verdict, what needs action, test cases with images, not tested
    bugs/BUG-1.md                 Jira-ready bug drafts (the run reply prints the acli command to file each)
    evidence/BUG-1.png            bug screenshots, problem boxed in red (labelled when Pillow is installed)
    evidence/BUG-1.webm           video of the bug's clean-state repro
    evidence/TC-1.png             one screenshot per checked case
    jira-comment-PROJ-1234.md     pass comment draft (PASS / PASS WITH NOTES): paste into Jira yourself
    repro/BUG-1.spec.ts           Playwright spec that fails while the bug exists
    .data/                        machine files: plan.json, run-<page>.json, requests-/console-<page>.log (temp snapshots pruned on teardown)
```

Each run creates a fresh folder. `qa-out/` and `.playwright-cli/` are added to `.git/info/exclude` automatically so run output stays out of git.

## Reading REPORT.md

`qa-mem report` renders `REPORT.md` from `.data/plan.json` and `.data/run-*.json`; agents never edit it directly. Top to bottom:

- **Header and verdict:** single ticket gets a verdict line with counts (bugs, open questions, cases needing attention, evidence images) and a link to the Jira pass-comment draft when eligible. Two or more tickets get a summary table (ticket, verdict, why, cases pass/fail/other, bugs).
- **Needs your action:** bugs to file (draft, image, video, repro and network links, ticket column aligned with the verdict, plus a folded *Filing commands & attachments* block with shell-safe `acli jira workitem create` commands, attachment paths and `qa-mem bug-status <hash> FILED <JIRA-KEY>` follow-ups for CONFIRMED drafts; a bug memory already links to a Jira key gets a `comment create` on that key instead, a new one tied to a ticket gets a `link create` when `jiraBugLinkType` is set, and saved duplicate hits show above the command), open questions (blocking first; duplicates across pages merged), failed/blocked/not-run cases, and browser signals no bug explains: failed requests (4xx, 5xx, network errors) and console errors the explorer saved before closing the browser (`.data/requests-<page>.log`, `console-<page>.log`) that neither its run JSON nor a bug draft names (a request by its method and path, `POST /api/zones`, or `GET /api/zones/:id` for one record's: the page's own path, a passing fact or a passing note's `GET /api/zones` explains no other call; only bugs, notes, cases that didn't pass and drafts explain a request; a request loop is a GET made 5 times), each once with a count. A signal first seen in this run is marked `[NEW]`, one seen before says in how many runs, and one you marked noise (`qa-mem signal-ignore`) is left out. Failures on hosts outside `allowedHosts` (a CDN, map tiles) are folded into one count line, and the same call made 5+ times is listed as a possible request loop. Signals never change a verdict. Test records the explorer couldn't delete (`Left in the app:` in its notes, e.g. when the budget ran out) are listed as *Test data left in the app*. Then bug drafts in `bugs/` that no run file lists or case names (a later run file, e.g. from `--resume`, replaced the one that found them): listed, never counted in a verdict. When empty, prints "Nothing needs your action."
- **Per ticket:** acceptance criteria with status (partial `--retest` runs keep prior-build passes clearly marked as verified on the prior build and yield `PASS WITH NOTES`), then a compact test case table (ID, optional priority, AC, optional page, case, status, actual, evidence link). Steps, expected, actual, notes, and `evidence/TC-<n>.png` sit in folded `<details>` blocks (non-pass cases open by default; unannotated screenshots are flagged `(not highlighted)`). The test plan folds after the cases.
- **Not tested, Resolved questions & Exploration notes:** folded blocks by category (`safety`, `out_of_scope`, `not_reached`, `needs_setup`, `heuristic`, `other`), resolved questions with answers, and per-page notes with heuristic coverage icons (✅ checked, ❌ problem found, ⛔ could not be checked). `Problems reading this folder` appears if any JSON file was unreadable or malformed.
- **Plan-only reports** (no run yet) show the summary table and questions, without an action block.

**Verdict per ticket** (decided by the renderer, first match wins):
1. ❌ FAIL: any case FAIL, or a CONFIRMED bug with severity CRITICAL or HIGH.
2. ⛔ BLOCKED: any case BLOCKED or not run, no cases at all, a case id another plan case repeats (its result can't be told apart), or a run on one of its pages that ended BLOCKED.
3. ⚠️ PASS WITH NOTES: any QUESTION case, an open `[BLOCKING]` plan question (one whose `ticket` names this ticket, or names no plan ticket), any other bug (MEDIUM or LOW, or SUSPECTED), or an AC with no test case.
4. ✅ PASS: everything else.

Before any run file exists, every ticket shows ⏳ NOT STARTED. A case belongs to the ticket its `ticket` field names (case and spaces ignored); in a one-ticket plan, a case naming none belongs to that ticket. A ticket's bugs are those tagged with its key, those named by its cases (by `BUG-n` or known-bug hash), and untagged bugs (including `"none"` or unknown tags) found on its cases' routes; an untagged bug named by another ticket's case belongs to that ticket only.

**Jira pass comment (`jira-comment-<KEY>.md`):** written next to `REPORT.md` for each PASS or PASS WITH NOTES ticket. Plain text: a `CHECK BEFORE POSTING` block (flags unknown/unreadable builds, lists screenshots to attach, and names any missing placeholders), followed by `"Passed on <release names>"`, `"Fixed: <note>"`, and one `TC-n - RESULT: title` line per case. Release names come from `releaseTemplates` in config (`{version}` expands from `fix_version`, e.g. `3.5` → `3.5.0`); the note comes from `fixed_note` in `plan.json`. The renderer never posts to Jira and rewrites (or removes) the draft on every render, so remove the CHECK block and fill placeholders in Jira or in a copy.

**Project safety rules (optional):** create `<project>/qa-rules.md` with one rule per line (e.g. "never click Confirm", "use only test device X"). Every explorer treats them as hard limits and logs skipped checks under *Not tested → Safety rules*.

## Config: `~/.config/qa-sdet/config.json`

The first run (or `/qa-sdet:qa-setup`) prompts for these if the file is missing:

```json
{
  "app": "traffic-ui",
  "qaBaseUrl": "https://qa.example.com",
  "allowedHosts": ["qa.example.com", "api.qa.example.com"],
  "ssoHost": "login.microsoftonline.com",
  "jiraProject": "PROJ",
  "jiraKeyPrefix": "PROJ",
  "mapRoute": "/map",
  "buildSource": {"type": "selector", "value": "footer .version"},
  "sourcePaths": ["/home/you/work/traffic-ui", "/home/you/work/traffic-api"]
}
```

- `jiraKeyPrefix`: project key used to expand bare ticket numbers.
- `jiraAcField` (optional): Jira custom field id for Acceptance Criteria (e.g. `customfield_10042`), included automatically by `qa-mem prep` and ticket reads.
- `inboxJql` (optional): custom read-only JQL query for `/qa-sdet:qa-inbox` (e.g. a shared QA queue instead of `assignee = currentUser()`).
- `browser`: `chrome` or `msedge` (installed system Chrome/Edge), or `firefox` / `webkit` (Playwright's own builds via `playwright-cli install-browser firefox`). Leave unset on first run to choose from `qa-mem browsers`.
- `releaseTemplates` (optional): release name patterns for Jira pass comments, e.g. `["Release-A-{version}", "Release-B-{version}"]`. Local config only.
- `buildSource.type`: `selector` (element text), `url` (version endpoint), or `header` (response header). Used to mark older memory stale after deploys.

`qa-mem doctor` checks every tool and config field and prints `FIX <what> -> <fix>` for gaps. `/qa-sdet:qa-setup` runs it automatically; other skills run it only after config edits or tool failures and ask before applying config fixes.

## Fallbacks

- **Jira:** `acli`, or ticket text you paste. There is no Jira MCP path.
- **Browser:** `playwright-cli` only. Without it, runs stop and print the install command.

## Work laptop / auto mode

In auto mode a classifier reviews commands before they run and blocks many of qa-sdet's. `/qa-sdet:qa-setup` (or `qa-mem setup-permissions --apply`) adds these allow rules to `~/.claude/settings.json`, after backing it up:

```
Bash(qa-mem *)
Bash(playwright-cli -s=qa *)
Bash(playwright-cli -s=qa-login *)
Bash(playwright-cli --version)
Bash(command -v playwright-cli*)
Bash(acli jira auth status*)
Bash(acli jira workitem view *)
Bash(acli jira workitem search *)
Bash(acli jira workitem comment list *)
Bash(playwright-cli kill-all*)
Bash(playwright-cli close-all*)
Skill(qa-sdet:*)
Read(~/.config/qa-sdet/config.json)
```

- Matching calls skip the prompt and classifier, including inside explorer and mapper subagents. Auto mode drops broad `Agent` and wildcarded interpreter rules (`Bash(python3 *)`), which is why skills run `qa-mem` bare (via the plugin's `bin/` on `PATH`) and why launching subagents still passes the classifier.
- The guard hook still runs on every call, auto-approving only calls it fully verified (see Safety below).
- If auto mode blocks `--apply` itself (it modifies `~/.claude/settings.json`), run `! qa-mem setup-permissions --apply` directly in the Claude Code prompt, then restart Claude Code. If settings are IT-managed, ask IT to add the rules above.
- Check `/permissions` → *Recently blocked* and `<project>/qa-out/guard-denials.log` whenever something is unexpectedly denied.

## Models

The agents run on Haiku: `qa-explorer` and `qa-mapper` with `effort: high`, and `qa-crawler` (the `qa-init --crawl` verify pass, a short prompt of its own) with `effort: medium`. `qa-reviewer` runs on Sonnet only when you pass `--verify` to `qa-ticket` or `qa-explore`: it reads the serious bug drafts (CONFIRMED CRITICAL/HIGH, SUSPECTED, or with `audit-bugs` problems) and REPORT.md marks each `reviewed ✓` or `⚠ check before filing: …`; it never changes a verdict. In side-by-side fixture runs, Haiku caught the same planted bugs as Sonnet at roughly 1/12 of the explorer cost. To switch, change `model:` in the agent's file under `agents/`.

## Memory DB

Stored at `~/.local/share/qa-sdet/<app>.db` (SQLite, WAL mode, FTS5). Core tables: `pages`, `facts`, `locators`, `coverage`, `bugs`, `runs`, `codemap`. Per-page caps keep the freshest 100 facts, 60 locators, 400 coverage rows, and 200 code-map rows (route rows and backend `*` rows are uncapped), plus the newest 2,000 runs; bugs are never pruned.

- **Auto-ingest:** agents never write SQL. Explorers write `qa-out/<run>/.data/run-<page>.json` (`references/run-json.md`), mappers write `qa-out/init/code-map-<area>[-<n>].json` (`references/code-map.md`), and the `Stop`/`SubagentStop` hooks ingest changed files under a file lock (skipping Python entirely when no `qa-out/` directory exists). A `PostToolUse` hook checks each file as it is written, while the agent can still fix it: a bug draft or repro spec against `references/bug-draft.md`, a run JSON against the shape ingest needs, a `plan.json` for repeated or missing case ids, a code map at once for its mapper; it also tells an explorer whose same `playwright-cli` command failed 3 times in a row to change approach. When an explorer stops, `SubagentStop` checks its run once (run file, signal logs, a status for every case in its brief, an `actual` on every PASS and FAIL and a `bug` on every FAIL, every draft listed, no critical `audit-bugs` problem) and sends it back one turn with what's missing. Agents have turn limits (`maxTurns`: explorer 45, mapper 25, crawler 20); the explorer writes an `INCOMPLETE` run file after its cases and each confirmed bug, so a run cut short keeps its results.
- **Bug dedup & staleness:** bugs deduplicate by a hash of normalized route + symptom; repeats increment `seen_count` once per run; a page's card also lists open and fixed bugs from pages that call the same endpoint (per the code map; endpoints more than 5 other pages call are skipped), so the explorer re-checks them and reports a sighting under the same hash; a `FIXED` bug seen again turns `REGRESSED` (statuses: `OPEN`, `FILED`, `FIXED`, `INVALID`, `REGRESSED`). Coverage from runs with an unreadable build expires after 7 days.
- **Coverage on the card:** `qa-mem card` lists the heuristics no run tried on the current build (`UNTRIED`) and the page's code-map rule fields that no coverage target names (`UNTESTED TARGETS`; a target `Speed limit field` covers the rule `speed`). An explore run finishes both lists, so one tested field no longer hides the rest. Each `PREVIOUSLY FIXED` bug is re-checked once per build (its line then ends `re-checked on build <b>`); seen again, it turns `REGRESSED`.
- **Code map (`/qa-sdet:qa-init`):** `qa-mem codemap-scan` deterministically selects source files (skipping `node_modules`, build output, vendor dirs, lockfiles, minified bundles, symlinks, large files, non-UTF-8 filenames, and secret stores). Large areas split into ~40-file / 60k-token chunks (up to 6 parallel mappers); app-shell menu configs (`*config.json` with `nav`/`module`/`key`) are parsed directly into route rows without a mapper. Re-runs scan only files whose content changed (or all of frontend + tests when a router changes; a page renamed inside its own single-route file or a new file-routed page reusing unchanged components needs `--full`). Code is treated as hints, never an oracle.
- **Manual CLI commands:** `qa-mem card <route|name>`, `qa-mem gaps [--changed] [--top N] [--json]` (every page, the most urgent to explore first, in tiers: source changed since its last visit per `git log` of its code-map files, a regressed bug, or an un-rechecked `FIXED` bug; then never-explored pages with a form, rule or write call (the crawler's read-only map doesn't count as exploring); then pages verified on an older build; then the rest. Within a tier the page's bug history (hotspot score) and its untried heuristics and untested fields order it; a source change is a hint, since HEAD may not be deployed yet; a detail route such as `/incidents/:id` shows the page it is opened from, through up to 4 hops (`/incidents/:id/history` is `open from /incidents: row click, then History tab`; `--json` gives `entry_route`, `entry_trigger` and `entry_chain`), and is marked `(needs a record id)` and left out of `Next:` and `--json` pages when no chain from a page without an id is known), `qa-mem code [route|--repos]`, `qa-mem codemap-forget <folder>`, `qa-mem signals-triage <run dir> --slug <slug>` (the explorer's pre-close check: app signals no bug explains yet, with their request numbers for `response-body`), `qa-mem audit-bugs <run dir>` (bug drafts and repro specs against `references/bug-draft.md`: a spec without `expect(`, a missing file or a draft hash memory doesn't store is critical; REPORT.md shows `repro ✓` or `⚠ <problem>` next to each bug), `qa-mem init-status <init dir>` (which mapper chunks of the newest scan are settled, and which to map again with why), `qa-mem bugs [STATUS]`, `qa-mem bug-status <hash-prefix> <STATUS> [JIRA-KEY]`, `qa-mem bug-sync [--jql]`, `qa-mem session-check [--record] [--build]`, `qa-mem dispatch <run> --route <route> [--mode] [--focus] [--budget N] [--minutes M] [--faults] [--api-writes] [--known-bugs] [--open-record [--target-route <detail route>]] [--hypothesis '<why this page>'] [--max-turns N]` (one explorer's setup: run marker, unique slug and `.data/brief-<slug>.md` with the route's cases, its tickets' AC, `qa-rules.md`, the charter, the card and, with `--hypothesis`, one `HYPOTHESIS:` line (scrubbed, at most 400 characters, labelled test data) saying why the page was picked; `--target-route` makes `--open-record` open that exact detail page, even one behind another), `qa-mem page-start <url>` (the explorer's start: open, sign-in check and build in one call; JSON with the `browser` it opened, and `block_scope` when blocked), `qa-mem api-probe <GET|HEAD|OPTIONS|POST|PUT|PATCH|DELETE> <url> [--json '<body>'] [--content-type T] [--header 'Name: value']` (one request from the signed-in browser, no cookie or token read: JSON `{status, ok, headers, body}` with contract headers only and a scrubbed body cut at 4000 characters; `allowedHosts` only, never login/session/token paths or `Authorization`/`Cookie` headers; writes need a run started with `--api-writes` and touch only `qa-sdet-` records), `qa-mem evidence <png> (--selector <css> | --ref <ref>) --label <text>` (an evidence image with its box and label in one call), `qa-mem annotate <png> [--box x,y,w,h ...] --label <text>` (a label alone is a caption in the quietest corner), `qa-mem plan-find <project> (--keys K1,K2 | --run DIR)` (also finds one ticket's part of a multi-ticket run), `qa-mem explore-next <project> [--run DIR]` (an explore run's unfinished routes, leads and pages its bugs may reach; `frontier` ranks them with a `hypothesis` each: every page calling a CONFIRMED bug's exact endpoint however many call it, the readers of what a write bug saved, pages sharing a call or component with a bug that names no endpoint, then leads, each detail route with the `entry_route` and `entry_chain` that opens it; `run_patterns` are the bug patterns the run has found so far; `expand_recommended` / `expand_reason` say whether pages beyond the unfinished routes are worth running: a HIGH lead, a CONFIRMED bug that may reach other pages or a new 5xx, else false, and `--adaptive` / `--continue` stop at the pages asked for), `qa-mem next-action <run dir> <slug>` (after each explorer: continue, recover the sign-in, or stop), `qa-mem spend <run dir> [--total-budget N] [--total-minutes M] [--more-budget N] [--more-minutes M] [--budget N] [--minutes M]` (what the run's explorers spent so far, from the `spend.jsonl` each teardown writes, and the `budget` / `minutes` to dispatch the next page with so the run's cap holds; `exhausted` when under 10 commands or 2 minutes are left), `qa-mem trace <project> [--keys K1,K2] [--fix-version VER]` (each ticket's latest verdict, AC coverage, build, bugs and retest across runs: a release sign-off table), `qa-mem prep <project> <keys...>`, `qa-mem history [route] [--builds N] [--json]` (build over build: runs, case results, bugs new / fixed / regressed; or a page's timeline), `qa-mem hotspots [--top N] [--json]`, `qa-mem impact <route|name|METHOD /path> [--json]` (what a change reaches, from the code map: an app-shell route's module, pages writing the same resource, readers of what it writes, pages rendering the same component file, and rules of one field that differ between sibling writers; cards show them as RELATED PAGES and CROSS-PAGE RULES DIFFER, `prep` as `context.impacted_routes`; hints, never proof), `qa-mem bug-match <route> <symptom> [--endpoint <METHOD /path>]` (likely duplicates before a new draft: reworded, on a sibling page or the same endpoint, NOT A BUG and fixed ones included), `qa-mem patterns [--min-pages N] [--json]` (one kind of bug on 2+ pages, from the `heuristic`/`pattern` explorers record), `qa-mem questions [open|resolved|all]`, `qa-mem answer <id> <answer>` (answered questions show on cards as DECISIONS), `qa-mem remember <route|*> <kind> <text> [--build-scoped]` (a pinned fact of your own), `qa-mem forget fact <id> | page <route> [--force]`, `qa-mem stale <build>`, `qa-mem search "<words> [-exclude]"` (stemmed; facts, bugs and the code map; relaxed to 2 of 3+ words when nothing matches all), `qa-mem card <route> [--focus <terms>]`, `qa-mem where`.

## Safety (hook: `hooks/guard.py`, `PreToolUse` on every tool)

The guard prevents mistakes; it is not a sandbox. It inspects command text in a single pass without emulating a shell and blocks calls crossing these hard lines:

1. **Jira writes:** any `acli` command except reads (`view`, `search`, `list`, `get`, `auth login|status`, `--help`, `--version`), `acli` with flags before command words (`acli --profile p jira …` — put flags after), `acli` invoked from inline code, and any non-read Atlassian/Jira/Confluence MCP tool.
2. **Other hosts and browsers:** `playwright-cli` navigation (`goto`, `open`, `tab-new`), `qa-mem browser-open`, `page-start` and `api-probe`, and `eval` URLs outside `allowedHosts` (add `localhost` only if testing locally); alternate browsers/configs (`--config`, `--extension`, `--cdp`, `--endpoint`, `attach`, `install` (writes a workspace config or skills), `PLAYWRIGHT_MCP_*`/`PWTEST_*` vars); `show --host` on non-local addresses; `--profile`/`--user-data-dir` except `open` with the main profile.
3. **Session secrets:** `cookie-*`, `localstorage-*`/`sessionstorage-*` reads, full request/response headers, `tracing-start`, `state-save`, `run-code`, `delete-data`, and `eval` reading cookie/storage/IndexedDB values (key names, `.length`, and searching the word "cookie" in page text are allowed); any access to `~/.local/share/qa-sdet/profiles/` outside `qa-mem browser-open` (including `Grep` content searches or recursive `**` `Glob` patterns rooted at `~` or `~/.local`); `playwright-cli upload`/`drop` of secret files or globs. The explorer and crawler (not your own session) may not run `cookie-clear`, `cookie-delete`, `state-load` (they would sign the QA browser out or swap its session) or `webmcp-call` (a tool the page registered).
4. **`qa-explorer` and `qa-crawler` restrictions** (the `qa-reviewer` may only `Read` the project's `qa-out/` and the plugin's references): any tool but `Bash`, `Read`, `Write`, `Grep`, `Glob` (no MCP server of your session); `Write` outside its run files (`<project>/qa-out/<run>/bugs|repro|evidence/<file>` and `.data/` `run-*.json`, `requests-`/`console-*.log`, `map-<n>.png`), so never source files, `REPORT.md`, `plan.json` or the run marker; writing, moving or removing the run marker (`qa-out/.active`, and `qa-out/.active.progress` beside it) by any command, including `qa-mem marker` and `qa-mem dispatch` (it sets the explorer's own budget, deadline and fault injection); redirections, `playwright-cli` files (`--filename`, video) and `qa-mem evidence` images outside its run files; the user's manual memory writes (`qa-mem bug-status`, `stale`, `answer`, `remember`, `forget`, `signal-ignore`, `signal-unignore`, `codemap-forget`); modifying qa-sdet config or `.playwright/cli.config.json`; starting a new browser with `playwright-cli open` (no QA sign-in, and it drops the request and console history the run saves; `goto` navigates); network tools other than HTTP clients (`curl`, `wget`, …) targeting `allowedHosts` without proxies, resolve overrides, curl `--variable`, or option/URL files (including glued short flags `-x`, `-K`, `-i`, `-e`, `--url=`, and `CURL_HOME`/`WGETRC`/`XDG_CONFIG_HOME` pointing at another rc file); downloads over the config; running script files via a shell, an interpreter or by path (`bash x.sh`, `python3 x.py`, `source x`, `./x.sh`; only the plugin's own `bin/qa-mem` and programs in `/usr`, `/bin`, `/sbin`, `/opt`, `/snap` are excepted); inline code given to an interpreter (`python3 -c`, `node -e`, `perl -e`, a pipe or heredoc into `python3`: it could rewrite the run marker or reach another host; `--version` and `python3 -m <module>` stay allowed, and so does `awk`); installing or running packages (`npm`, `npx`, `pip`, `uvx`, …), which download code from other hosts and run it.
5. **Memory DB protection:** deleting, moving, overwriting, or unpacking into `~/.local/share/qa-sdet/` (or removing parent directories), including via `exec`/`builtin`, chained redirections, `find -L`, or `cp`/`mv` backup options; `sqlite3` on the DB unless `-readonly`.
6. **Run limits:** enforced from `qa-out/.active` (written by `qa-mem marker start`; ignored after 8 h or if malformed). Blocks browser commands past your `--budget` or `--minutes` (`close`, `requests` and `console` always work; a `PostToolUse` hook warns the explorer once when 15% of the budget (at least 3 commands) or 2 minutes are left, so it deletes its test records while it still can; one call can't spend past that line at once (`send at most N now`), and if the warning hasn't reached it yet the guard refuses the first call past it once, with the warning) and `playwright-cli` inside loops or shell functions when a budget is set. A run with no budget or time still ends at the agent's `maxTurns` (45 for the explorer, 20 for the crawler): the same hook counts the agent's Bash and Write calls in `qa-out/.active.progress` (beside the marker, so the explorer can't change it; a Read or Grep isn't seen, so the line is drawn early) and warns once, 8 or a quarter of the turns before the end, to go to step 9 (the crawler: step 4) before it is cut off cold. `qa-mem marker start` and `dispatch` take `--max-turns N` to change the count (a test run with a small limit). The same file lets the hook spot a spin: 8 browser calls in a row that open no new page state, send no new interaction (one not repeated within the last 12 calls) and write no file get one nudge to change approach, and a call that failed 3 times since the agent last got anywhere (a successful `snapshot` in between doesn't count as getting anywhere) hears to change approach, whether the failures were in a row or alternated with looks. `qa-mem evidence` counts as 2 (4 with `--ref`); `qa-mem api-probe` counts as 1 (2 for a PUT, PATCH or DELETE, which reads the record first). A call Claude Code's auto mode denies after the guard counted it is given back; one you reject at a prompt stays counted (no hook runs for it). Without a marker, nothing is limited. Fault injection (`route`, `network-state-set`) always requires a marker with `--faults`. A POST, PUT, PATCH or DELETE through `qa-mem api-probe` always requires a marker with `--api-writes` (set by the `qa-explore` / `qa-ticket` flag of that name, through `qa-mem dispatch`), and the `Authorization`, `Cookie` and `Proxy-Authorization` headers are refused. For every role, `playwright-cli eval` may not send a write itself (`fetch` with a POST, PUT, PATCH or DELETE method, `XMLHttpRequest`, `sendBeacon`): `api-probe` is the only write path. That check is pattern-based (a method built at run time passes); UI form submits are unaffected.
7. **`qa-mapper` restrictions:** read-only tools plus `Write` strictly to `<project>/qa-out/init/code-map-<area>[-<n>].json`; blocks reading secret files, credential folders (`~/.ssh`, `~/.aws`, …), and qa-sdet data/config.
8. **Auto-approval & denial log:** calls fully checked in one pass — `playwright-cli` (writing only inside `qa-out/`), strictly read-only `acli` (`jira auth status`, `workitem view|search|comment list`, excluding `--web`), the plugin's `qa-mem` (excluding `setup-permissions --apply`), and `head`/`tail`/`grep` used as stdin filters or reading only inside `qa-out/` or `.playwright-cli/`, joined by `;`, `&&`, `||`, `|` with redirections only to `qa-out/` or `/dev/null` — are auto-approved to skip prompts. Every guard denial and qa-sdet-related auto-mode denial (`PermissionDenied` hook) is logged to `<project>/qa-out/guard-denials.log` with secrets masked (newest 1,000 lines).

**Run guarded commands directly:** when a call names `playwright-cli`, `acli`, `qa-mem`, or the data/profile folder and obscures what runs (program/verb from `$VAR` or `$(...)`, `eval "$X"`, piped/sourced scripts, inline Python/Node/awk code, `xargs`, command strings passed to launchers like `ssh`/`watch`/`trap`, computed paths near the data folder, or nesting deeper than 4), the guard denies it rather than guessing.

**Fail-closed fallback:** if the guard throws an unexpected error, or a command over 100 KB names a guarded tool/path, it denies calls mentioning a guarded tool/path (and any `qa-mapper` call) and passes unrelated commands through.

**Known guard limits:** checks command text, not runtime intent. It does not inspect scripts on disk (`bash build.sh` in the main session runs whatever is in that file), obfuscated shell text passes (`$'\x70…'`, split variable names, brace paths), `qa-mapper` may content-search a folder containing `.env` files, and a Playwright MCP server is not guarded (do not install one for qa-sdet). Folder walkers started at HOME itself (`grep -r x ~`, `find ~ -exec …`, `tar c ~`) are not blocked, so `cd ~ && git status` keeps working; `git -c alias.x='!cmd'` runs `cmd` unchecked; the `qa-explorer` can still reach other hosts from inline Python/Node code, `dig` or `git clone`.

## Privacy

- **Login:** your QA session lives in `~/.local/share/qa-sdet/profiles/main` (mode `700`). Only the browser reads it; the guard blocks all other access. `qa-mem browser-open` strips every `PLAYWRIGHT_MCP_*` and `PWTEST_*` env var so personal cookies never enter the QA browser. To sign out or reset the profile, run `playwright-cli -s=qa close` and `playwright-cli -s=qa-login close`, then delete `~/.local/share/qa-sdet/profiles/` in your own terminal. Uninstalling the plugin leaves `~/.local/share/qa-sdet/` and `~/.config/qa-sdet/` intact; delete them yourself if desired.
- **Page snapshots:** `playwright-cli` writes snapshots under `<project>/.playwright-cli/` (excluded from git); teardown removes the current run's snapshots and any older than 7 days.
- **Redaction:** ingest scrubs auth/cookie headers, known token formats (JWT, GitHub, Slack, AWS, GitLab, Atlassian, `sk-` keys, private keys, `Bearer` tokens), URL credentials, and token query parameters from Markdown/text files, plus secret-named keys and cookie objects in JSON. Repro specs lose literal tokens only so they remain runnable. Plain-prose passwords are not recognized — keep real passwords out of notes — and screenshots and videos cannot be scrubbed, so inspect them before attaching to Jira.
- **Source code:** `/qa-sdet:qa-init` runs locally; secret files are skipped and mappers write `[secret]` instead of literal values.

## Self-test

```sh
python3 -m unittest discover -s tests -v                  # qa-mem, code map, guard, report, docs
python3 -m http.server 8765 -d tests/fixture &            # fixture app with planted bugs
# needs a config with "qaBaseUrl": "http://localhost:8765" and "localhost" in allowedHosts
claude --plugin-dir . -p "/qa-sdet:qa-explore http://localhost:8765/form.html"
```

## Known limits

- One QA run at a time per machine: runs share the `qa` browser session, the `main` profile, and per-project `qa-out/.active` marker.
- Linux-only browser and Pillow hints (`qa-mem doctor` checks Linux Chrome/Edge paths and suggests `apt-get` on PEP 668 systems).
- The guard checks command text, not intent (see Safety); JS that constructs URLs or storage names dynamically (`'https:/' + '/evil.example'`, `window['local' + 'Storage']`) passes.
- Jira writes stay manual: run replies print the `acli` create command for each confirmed bug draft; attach images, videos and repro specs yourself.
