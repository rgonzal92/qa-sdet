# qa-sdet

A Claude Code plugin for SDET work: it reads your Jira tickets, writes a test plan and test cases, runs them in the QA app with `playwright-cli`, hunts bugs around them, and renders one readable report per run with a verdict per ticket. A local SQLite memory of the app updates itself after every run.

## Install (per machine)

**Needs:** Linux, Claude Code, Python 3.9+ as `python3` (hooks run it), and `playwright-cli` (`npm i -g @playwright/cli`).
**Optional:** `acli` signed in (`acli jira auth login`) for Jira reads, `git` for repo-aware code scans and `.git/info/exclude`, and Pillow for red highlight boxes on bug screenshots (`qa-mem doctor` and `/qa-sdet:qa-setup` print the install command that works on your system: `pip install --user`, or `apt-get` where system Python blocks `pip`).

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
| `/qa-sdet:qa-inbox` | Open tickets assigned to you, NEW since last look first; saved to `qa-out/INBOX.md` |
| `/qa-sdet:qa-ticket 1234 1250 [--faults] [--budget N] [--minutes M] [--review]` | Tests tickets end to end and gives each a verdict |
| `/qa-sdet:qa-plan 1234` or `"text"` | Test plan + test cases only, no browser; `qa-ticket` reuses them |
| `/qa-sdet:qa-explore <url \| route \| page name> [--faults] [--budget N] [--minutes M]` | Exploratory bug hunt on one page, no ticket |
| `/qa-sdet:qa-init [folders] [--full] [--crawl [--budget N] [--minutes M]]` | Maps routes, locators, validation rules and endpoints from source into memory; `--crawl` verifies locators live |
| `/qa-sdet:qa-memory <subcommand>` | Queries or updates memory (`card`, `search`, `bugs`, `bug-status`, `stale`, `hash`, `doctor`, `browsers`, `where`) |

- **Tickets:** bare numbers use `jiraKeyPrefix` (`1234` → `PROJ-1234`); full keys (`OPS-7`) work too; pass several separated by spaces or commas. Plain requests ("write test cases for 1234", "explore the closures page") also trigger the matching skill.
- **`qa-mem` CLI:** skills call `qa-mem` bare because the plugin's `bin/` directory is on the Bash `PATH` while enabled. Outside Claude Code (or if `qa-mem` is not on `PATH`), run `python3 <plugin folder>/bin/qa-mem`.
- **Optional limits:** runs continue until done unless you pass `--budget N` (browser commands) or `--minutes M` (up to 480). Limits apply per page in `qa-ticket`/`qa-explore` and to the whole crawl in `qa-init --crawl`; the guard hook enforces them.

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
    evidence/BUG-1.png            bug screenshots, problem boxed in red with a label
    evidence/TC-1.png             one screenshot per checked case
    jira-comment-PROJ-1234.md     pass comment draft (PASS / PASS WITH NOTES): paste into Jira yourself
    repro/BUG-1.spec.ts           Playwright spec that fails while the bug exists
    .data/                        machine files: plan.json, run-<page>.json (temp snapshots pruned on teardown)
```

Each run creates a fresh folder. `qa-out/` and `.playwright-cli/` are added to `.git/info/exclude` automatically so run output stays out of git.

## Reading REPORT.md

`qa-mem report` renders `REPORT.md` from `.data/plan.json` and `.data/run-*.json`; agents never edit it directly. Top to bottom:

- **Header and verdict:** single ticket gets a verdict line with counts (bugs, open questions, cases needing attention, evidence images) and a link to the Jira pass-comment draft when eligible. Two or more tickets get a summary table (ticket, verdict, why, cases pass/fail/other, bugs).
- **Needs your action:** bugs to file (draft and image links, ticket column aligned with the verdict), open questions (blocking first; duplicates across pages merged), and failed/blocked/not-run cases. When empty, prints "Nothing needs your action."
- **Per ticket:** acceptance criteria with status, then a compact test case table (ID, optional priority, AC, optional page, case, status, actual, evidence link). Steps, expected, actual, notes, and `evidence/TC-<n>.png` sit in folded `<details>` blocks (non-pass cases open by default; unannotated screenshots are flagged `(not highlighted)`). The test plan folds after the cases.
- **Not tested, Resolved questions & Exploration notes:** folded blocks by category (`safety`, `out_of_scope`, `not_reached`, `needs_setup`, `heuristic`, `other`), resolved questions with answers, and per-page notes with heuristic coverage icons (✅ checked, ❌ problem found, ⛔ could not be checked). `Problems reading this folder` appears if any JSON file was unreadable or malformed.
- **Plan-only reports** (no run yet) show the summary table and questions, without an action block.

**Verdict per ticket** (decided by the renderer, first match wins):
1. ❌ FAIL: any case FAIL, or a CONFIRMED bug with severity CRITICAL or HIGH.
2. ⛔ BLOCKED: any case BLOCKED or not run, or no cases at all.
3. ⚠️ PASS WITH NOTES: any QUESTION case, any other bug (MEDIUM or LOW, or SUSPECTED), or an AC with no test case.
4. ✅ PASS: everything else.

Before any run file exists, every ticket shows ⏳ NOT STARTED. A ticket's bugs are those tagged with its key, those named by its cases (by `BUG-n` or known-bug hash), and untagged bugs (including `"none"` or unknown tags) found on its cases' routes; an untagged bug named by another ticket's case belongs to that ticket only.

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

Both agents (`qa-explorer` and `qa-mapper`) run on Haiku with `effort: high`. In side-by-side fixture runs, Haiku caught the same planted bugs as Sonnet at roughly 1/12 of the explorer cost. To switch, change `model:` in `agents/qa-explorer.md` or `agents/qa-mapper.md`.

## Memory DB

Stored at `~/.local/share/qa-sdet/<app>.db` (SQLite, WAL mode, FTS5). Core tables: `pages`, `facts`, `locators`, `coverage`, `bugs`, `runs`, `codemap`. Per-page caps keep the freshest 100 facts, 60 locators, 400 coverage rows, and 200 code-map rows (route rows and backend `*` rows are uncapped), plus the newest 2,000 runs; bugs are never pruned.

- **Auto-ingest:** agents never write SQL. Explorers write `qa-out/<run>/.data/run-<page>.json` (`references/run-json.md`), mappers write `qa-out/init/code-map-<area>[-<n>].json` (`references/code-map.md`), and the `Stop`/`SubagentStop` hooks ingest changed files under a file lock (skipping Python entirely when no `qa-out/` directory exists).
- **Bug dedup & staleness:** bugs deduplicate by a hash of normalized route + symptom; repeats increment `seen_count` once per run; a `FIXED` bug seen again turns `REGRESSED` (statuses: `OPEN`, `FILED`, `FIXED`, `INVALID`, `REGRESSED`). Coverage from runs with an unreadable build expires after 7 days.
- **Code map (`/qa-sdet:qa-init`):** `qa-mem codemap-scan` deterministically selects source files (skipping `node_modules`, build output, vendor dirs, lockfiles, minified bundles, symlinks, large files, non-UTF-8 filenames, and secret stores). Large areas split into ~40-file / 60k-token chunks (up to 6 parallel mappers); app-shell menu configs (`*config.json` with `nav`/`module`/`key`) are parsed directly into route rows without a mapper. Re-runs scan only files whose content changed (or all of frontend + tests when a router changes; a page renamed inside its own single-route file or a new file-routed page reusing unchanged components needs `--full`). Code is treated as hints, never an oracle.
- **Manual CLI commands:** `qa-mem card <route|name>`, `qa-mem code [route|--repos]`, `qa-mem codemap-forget <folder>`, `qa-mem bugs [STATUS]`, `qa-mem bug-status <hash-prefix> <STATUS>`, `qa-mem stale <build>`, `qa-mem search "<words> [-exclude]"`, `qa-mem where`.

## Safety (hook: `hooks/guard.py`, `PreToolUse` on every tool)

The guard prevents mistakes; it is not a sandbox. It inspects command text in a single pass without emulating a shell and blocks calls crossing these hard lines:

1. **Jira writes:** any `acli` command except reads (`view`, `search`, `list`, `get`, `auth login|status`, `--help`, `--version`), `acli` with flags before command words (`acli --profile p jira …` — put flags after), `acli` invoked from inline code, and any non-read Atlassian/Jira/Confluence MCP tool.
2. **Other hosts and browsers:** `playwright-cli` navigation (`goto`, `open`, `tab-new`), `qa-mem browser-open`, and `eval` URLs outside `allowedHosts` (add `localhost` only if testing locally); alternate browsers/configs (`--config`, `--extension`, `--cdp`, `--endpoint`, `attach`, `PLAYWRIGHT_MCP_*`/`PWTEST_*` vars); `show --host` on non-local addresses; `--profile`/`--user-data-dir` except `open` with the main profile.
3. **Session secrets:** `cookie-*`, `localstorage-*`/`sessionstorage-*` reads, full request/response headers, `tracing-start`, `state-save`, `run-code`, `delete-data`, and `eval` reading cookie/storage/IndexedDB values (key names, `.length`, and searching the word "cookie" in page text are allowed); any access to `~/.local/share/qa-sdet/profiles/` outside `qa-mem browser-open` (including `Grep` content searches or recursive `**` `Glob` patterns rooted at `~` or `~/.local`); `playwright-cli upload`/`drop` of secret files or globs.
4. **`qa-explorer` restrictions:** modifying qa-sdet config or `.playwright/cli.config.json`; network tools other than HTTP clients (`curl`, `wget`, …) targeting `allowedHosts` without proxies, resolve overrides, or option/URL files (including glued short flags `-x`, `-K`, `-i`, `-e` and `--url=`); running script files via a shell or interpreter (`bash x.sh`, `python3 x.py`, `source x`; only the plugin's own `bin/qa-mem` is excepted).
5. **Memory DB protection:** deleting, moving, overwriting, or unpacking into `~/.local/share/qa-sdet/` (or removing parent directories), including via `exec`/`builtin`, chained redirections, `find -L`, or `cp`/`mv` backup options; `sqlite3` on the DB unless `-readonly`.
6. **Run limits:** enforced from `qa-out/.active` (written by `qa-mem marker start`; ignored after 8 h or if malformed). Blocks browser commands past your `--budget` or `--minutes` (`close` always works) and `playwright-cli` inside loops when a budget is set. Without a marker, nothing is limited. Fault injection (`route`, `network-state-set`) always requires a marker with `--faults`.
7. **`qa-mapper` restrictions:** read-only tools plus `Write` strictly to `<project>/qa-out/init/code-map-<area>[-<n>].json`; blocks reading secret files, credential folders (`~/.ssh`, `~/.aws`, …), and qa-sdet data/config.
8. **Auto-approval & denial log:** calls fully checked in one pass — `playwright-cli` (writing only inside `qa-out/`), the plugin's `qa-mem` (excluding `setup-permissions --apply`), and `head`/`tail`/`grep` used as stdin filters or reading only inside `qa-out/` or `.playwright-cli/`, joined by `;`, `&&`, `||`, `|` with redirections only to `qa-out/` or `/dev/null` — are auto-approved to skip prompts. Every guard denial and qa-sdet-related auto-mode denial (`PermissionDenied` hook) is logged to `<project>/qa-out/guard-denials.log` with secrets masked (newest 1,000 lines).

**Run guarded commands directly:** when a call names `playwright-cli`, `acli`, `qa-mem`, or the data/profile folder and obscures what runs (program/verb from `$VAR` or `$(...)`, `eval "$X"`, piped/sourced scripts, inline Python/Node/awk code, `xargs`, command strings passed to launchers like `ssh`/`watch`/`trap`, computed paths near the data folder, or nesting deeper than 4), the guard denies it rather than guessing.

**Fail-closed fallback:** if the guard throws an unexpected error, or a command over 100 KB names a guarded tool/path, it denies calls mentioning a guarded tool/path (and any `qa-mapper` call) and passes unrelated commands through.

**Known guard limits:** checks command text, not runtime intent. It does not inspect scripts on disk (`bash build.sh` in the main session runs whatever is in that file), obfuscated shell text passes (`$'\x70…'`, split variable names, brace paths), `qa-mapper` may content-search a folder containing `.env` files, and a Playwright MCP server is not guarded (do not install one for qa-sdet).

## Privacy

- **Login:** your QA session lives in `~/.local/share/qa-sdet/profiles/main` (mode `700`). Only the browser reads it; the guard blocks all other access. `qa-mem browser-open` strips every `PLAYWRIGHT_MCP_*` and `PWTEST_*` env var so personal cookies never enter the QA browser. To sign out or reset the profile, run `playwright-cli -s=qa close` and `playwright-cli -s=qa-login close`, then delete `~/.local/share/qa-sdet/profiles/` in your own terminal. Uninstalling the plugin leaves `~/.local/share/qa-sdet/` and `~/.config/qa-sdet/` intact; delete them yourself if desired.
- **Page snapshots:** `playwright-cli` writes snapshots under `<project>/.playwright-cli/` (excluded from git); teardown removes the current run's snapshots and any older than 7 days.
- **Redaction:** ingest scrubs auth/cookie headers, known token formats (JWT, GitHub, Slack, AWS, GitLab, Atlassian, `sk-` keys, private keys, `Bearer` tokens), URL credentials, and token query parameters from Markdown/text files, plus secret-named keys and cookie objects in JSON. Repro specs lose literal tokens only so they remain runnable. Plain-prose passwords are not recognized — keep real passwords out of notes — and screenshots cannot be scrubbed, so inspect them before attaching to Jira.
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
- Jira writes stay manual: run replies print the `acli` create command for each confirmed bug draft; attach images and repro specs yourself.
