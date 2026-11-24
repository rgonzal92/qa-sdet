# qa-sdet

A Claude Code plugin for SDET work: it reads your Jira tickets, writes a test plan and test cases, runs them in the QA app with playwright-cli, hunts bugs around them, and gives you one readable report per run with a verdict per ticket. An SQLite memory of the app under test updates itself after every run.

## Install (per machine)

From GitHub (nothing to clone):
```sh
claude plugin marketplace add rgonzal92/qa-sdet
claude plugin install qa-sdet@qa-sdet
```
From a local clone (optional, any folder you like):
```sh
git clone https://github.com/rgonzal92/qa-sdet.git /any/folder/qa-sdet
claude plugin marketplace add /any/folder/qa-sdet     # absolute path
claude plugin install qa-sdet@qa-sdet
```
Inside Claude Code's `/plugin` command, `~` isn't expanded: type the full path (`/home/you/...`), or the command reads it as a bad `owner/repo`.

Needs: Claude Code, Python 3.9+ as `python3` (the hooks run it), `playwright-cli` (`npm i -g @playwright/cli`), optionally `acli` signed in (`acli jira auth login`) and Pillow, for the red boxes on bug images (`qa-mem doctor` and `/qa-sdet:qa-setup` give the install command that works on your system: `pip install --user`, or your OS package where the system Python refuses pip).

To update: `claude plugin marketplace update qa-sdet && claude plugin update qa-sdet@qa-sdet`, then restart Claude Code.

## Quick start

1. `/qa-sdet:qa-setup`: checks your tools, offers the Pillow install, asks your ticket prefix (e.g. `STUDIO`), and adds allow rules for auto mode.
2. `/qa-sdet:qa-login`: sign in to the QA app once (SSO + MFA in a browser window).
3. `/qa-sdet:qa-inbox`: your open Jira tickets, NEW ones marked.
4. `/qa-sdet:qa-ticket 1234 1250`: plan, cases, test run, report. Open the `REPORT.md` it prints.

## Commands

| Command | What it does |
|---|---|
| `/qa-sdet:qa-setup` | One-time setup: tool check, Pillow, ticket prefix, allow rules, old cookie file clean-up |
| `/qa-sdet:qa-login` | You sign in once in qa-sdet's own browser profile; runs reuse it |
| `/qa-sdet:qa-inbox` | Open tickets assigned to you, NEW since last look first; saved to `qa-out/INBOX.md` |
| `/qa-sdet:qa-ticket 1234 1250 [--faults] [--budget N] [--minutes M] [--review]` | Tests tickets end to end (below) and gives each a verdict |
| `/qa-sdet:qa-plan 1234` or `"text"` | Test plan + test cases only, no browser; qa-ticket reuses them |
| `/qa-sdet:qa-explore /route [--faults] [--budget N] [--minutes M]` | Exploratory bug hunt on one page, no ticket |
| `/qa-sdet:qa-init [folders] [--full] [--crawl [--budget N] [--minutes M]]` | Maps routes, locators, validation rules and endpoints from your source into the memory; `--crawl` confirms them in the QA app |
| `/qa-sdet:qa-memory card /route` | What's known about a page: facts, locators, coverage, open bugs |

Tickets: bare numbers use your `jiraKeyPrefix` (`1234` → `STUDIO-1234`); full keys (`OPS-7`) work too; several at once, separated by spaces or commas.
You can also ask in plain words ("write test cases for 1234", "explore the closures page").

### What qa-ticket does
1. **Analyze** each ticket: summary, numbered acceptance criteria (AC), related open Jira bugs.
2. **Plan** per ticket: scope, approach, risks, out of scope, test data.
3. **Test cases** TC-1..n, each tied to an AC, with steps and the expected result. The report is written now (every case "not run"); `--review` stops here for you to check; reply `go`, or run the same `/qa-sdet:qa-ticket` later without `--review` (your edits to the plan are kept).
4. **Test** with playwright-cli: one explorer per page, one after another.
5. **Track** a status per case: PASS, FAIL, BLOCKED, NOT_RUN or QUESTION, with what was seen and the bug it found.
6. **Report**: `REPORT.md`, re-rendered after every page.
7. **Verdict** per ticket: ✅ PASS · ⚠️ PASS WITH NOTES (minor or suspected bugs, open questions, or an AC with no test case) · ❌ FAIL (a failed case or a HIGH/CRITICAL confirmed bug) · ⛔ BLOCKED (cases blocked or not run, or no cases) · ⏳ NOT STARTED (plan written, no test run yet, e.g. after `--review`). Exact rules: `references/report.md`.

### Budget (optional)
Runs go until the job is done. `--budget N` (browser commands) and `--minutes M` (at most 480) set a limit per page (for `qa-init --crawl`, on the whole crawl); the guard hook enforces it.

## Output

```
qa-out/
  README.md                       index of all runs, newest first: date, run, verdicts, bugs
  INBOX.md                        from qa-inbox
  2026-11-23-STUDIO-1234+STUDIO-1250/
    REPORT.md                     open this: verdict, what needs action, test cases with images, not tested
    bugs/BUG-1.md                 Jira-ready bug drafts (the run's reply gives the acli command to file each)
    evidence/BUG-1.png            bug images, problem boxed in red with a label
    evidence/TC-1.png             one image per checked case
    jira-comment-STUDIO-1234.md   pass comment draft (PASS and PASS WITH NOTES): paste it into Jira yourself
    repro/BUG-1.spec.ts           Playwright spec that fails while the bug exists
    .data/                        machine files: plan.json, run-<page>.json, snapshots
```
Each run gets a fresh folder. `qa-out/`, `.playwright-cli/` and `.playwright-mcp/` are kept out of git (via `.git/info/exclude`).

Project safety rules (optional): write them in `<project>/qa-rules.md`, one per line ("never click Confirm", "use only test device X"). Every explorer gets them as hard limits; a check they hold back is listed under Not tested, Safety rules.

## Config: `~/.config/qa-sdet/config.json`

The first run (or `/qa-sdet:qa-setup`) asks for these if the file is missing.

```json
{
  "app": "traffic-ui",
  "qaBaseUrl": "https://qa.example.com",
  "allowedHosts": ["qa.example.com", "api.qa.example.com"],
  "ssoHost": "login.microsoftonline.com",
  "jiraProject": "STUDIO",
  "jiraKeyPrefix": "STUDIO",
  "mapRoute": "/map",
  "buildSource": {"type": "selector", "value": "footer .version"},
  "sourcePaths": ["/home/you/work/traffic-ui", "/home/you/work/traffic-api"]
}
```
- `jiraKeyPrefix`: what bare ticket numbers expand to.
- `browser`: `chrome`, `msedge` (your installed Chrome or Edge), `firefox` or `webkit` (Playwright's own builds: `playwright-cli install-browser firefox`). Leave it out: the first run lists what's installed (`qa-mem browsers`) and asks you to choose.
- `buildSource.type`: `selector` (text of an element), `url` (a version endpoint) or `header` (a response header). The build keeps memory honest: knowledge from an older build is marked stale and re-verified.

`qa-mem doctor` lists what works and the fix for each gap (`FIX <what>  -> <fix>`). qa-setup, qa-login, qa-ticket, qa-plan, qa-explore and qa-init run it after reading the config and ask you before applying a config fix it prints (a `qaBaseUrl` without `https://`, an `ssoHost` given as a URL, a `jiraKeyPrefix` like `STUDIO-1234`); a run stops while one stays open.

## Fallbacks
- **Browser:** `playwright-cli`, then any Playwright MCP you have in Claude Code (tools `mcp__<server>__browser_*`); with neither, runs stop with install steps. With the MCP browser, runs use that browser's own login: the skill asks you to sign in in its window when needed (it must run headed). Those runs make no recordings (repro specs are written from the steps), and fault injection only works if the MCP exposes route/offline tools (e.g. `--caps=network`).
- **Jira:** `acli`, then any Atlassian/Jira MCP (read tools only), then ticket text you paste.

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
Skill(qa-sdet:*)
Read(~/.config/qa-sdet/config.json)
```
Calls these rules match skip the prompt and the classifier. Auto mode ignores rules for agents and for interpreters with wildcards (`Bash(python3 *)`), which is why the skills run the plugin's `qa-mem` command bare (the plugin's `bin/` folder is on the Bash PATH): starting an explorer or mapper still goes past the classifier. If auto mode blocks the `--apply` step itself (it edits Claude Code's own settings), type `! qa-mem setup-permissions --apply` in the prompt to run it as your own command. The plugin's guard hook still runs on every call and blocks the unsafe ones (Safety below). Restart Claude Code afterwards. If your company manages settings centrally, ask IT to add the rules there.

## Models
Both agents (the explorer and the `qa-init` code mapper) run on Haiku with high effort. In a side-by-side fixture run, Haiku found the same planted bugs as Sonnet at about 1/12 of the explorer cost. To switch back, set `model: sonnet` in `agents/qa-explorer.md`.

## Memory DB

`~/.local/share/qa-sdet/<app>.db` (SQLite, WAL, FTS5). Tables: pages, facts, locators, coverage, bugs, runs, codemap. Capped per page at the freshest 100 facts, 60 locators and 400 coverage rows (2000 runs total); bugs are kept.
Agents never write SQL: each explored page writes `qa-out/<run>/.data/run-<page>.json` (schema: `references/run-json.md`) and the plugin's `Stop`/`SubagentStop` hook ingests it. A file is ingested again only when its content changes. Bugs dedup by a hash of route + symptom; repeats bump `seen_count` once per run; a FIXED bug seen again becomes REGRESSED. Statuses: OPEN, FILED, FIXED, INVALID, REGRESSED. Run folders from older versions are still read.
Coverage from a run that couldn't read the build counts for 7 days, then the heuristic is retried.
Code map (`/qa-sdet:qa-init`): `qa-mem codemap-scan` lists the source files deterministically (skipping `node_modules`, build output, lockfiles and minified bundles), one `qa-mapper` agent per area reads only those (a large area is split into chunks of about 40 files / 60k tokens, up to 6 mappers at a time), and the hook ingests their `qa-out/init/code-map-<area>[-<n>].json` into the `codemap` table. In a git repo only tracked and not-ignored files are read; links, very large files and secret stores (`.env*`, keys and certificates, `secrets.*`/`credentials.*`/`config.*` files, config modules holding credentials) never are. Re-runs read only files whose content changed (the whole frontend and tests when a router changed; a new file-routed page that reuses unchanged components gets their locators with `--full`). Code is never an oracle: a page that differs from the code is a question for you.
Manual: `qa-mem bugs`, `qa-mem bug-status <hash> FILED` (a prefix must match exactly one bug), `qa-mem stale <build>`, `qa-mem search "closure schedule"` (every word must match; `-word` excludes).

## Safety (hook: `hooks/guard.py`, PreToolUse on every tool)

Denied (everything else passes). Only words in command position are checked, so `echo acli create …` or a `grep` near the DB pass.
1. `playwright-cli` navigation (`goto`/`open`/`tab-new`/`qa-mem browser-open`, URLs in `eval`) to hosts not in `allowedHosts` (add `localhost` there only if you test something local); `run-code` (it runs Node), `attach`, and `show --host` on a non-local address.
2. `acli` anything except reads (`view`, `search`, `list`, `get`, `auth login|status`, `--help`, `--version`), and any acli command with a flag before its command words (`acli --profile p jira …`; write `acli jira … --profile p`): bug drafts end with the command for you to run.
3. During a run (marker `qa-out/.active`, written by `qa-mem marker start`, ignored after 8 h): browser commands past a budget or time limit you set (`close` always works), `route` / `network-state-set` unless the run used `--faults`, and every browser command if the marker is malformed. The explorer's browser commands (playwright-cli, `qa-mem browser-open`, Playwright MCP `browser_*`) need an active marker; closing the browser or its own tab always works.
4. Session secrets: `cookie-*`, `localstorage-*`/`sessionstorage-*` reads, full request/response headers, `tracing-start`, `state-save` (it dumps cookies), `delete-data` (it wipes the signed-in profile), `eval` reading cookie or web-storage values; any read of the browser profile folder `~/.local/share/qa-sdet/profiles/`, of the old `~/.config/qa-sdet/qa-state.json`, of the Playwright MCP's profiles (`~/.cache/ms-playwright-mcp`) or your `PLAYWRIGHT_MCP_STORAGE_STATE` cookie jar; `--profile`/`--user-data-dir` with any other folder.
5. Deleting, moving or overwriting the memory DB, or unpacking an archive into its folder; `sqlite3` on it unless `-readonly`.
6. qa-mapper: only reading tools, and writing only its `qa-out/init/code-map-<area>[-<n>].json`; no reads or content searches of secret files (`.env*`, keys and certificates, `secrets.*`/`credentials.*` files, `~/.ssh`, `~/.aws` and other credential folders, qa-sdet's data, config and browser profiles).
7. qa-explorer: writing outside its run folder's `bugs/`, `repro/`, `evidence/` (via screenshot) and `.data/`; browser output files (screenshots, snapshots, PDFs, console logs, videos) anywhere but `<run>/evidence/` and `<run>/.data/`; `qa-mem` admin commands (marker, report, inbox, setup-permissions, …); `annotate` outside `qa-out/*/evidence/`; network tools outside the browser (`curl`, `wget`, `ssh`, …); git commands that change the project (`clean`, `stash`, `checkout`, `commit`, …); dumping the page's HTML or scripts; other MCPs you've connected (mail, Jira, …).
8. Uploads (`playwright-cli upload`/`drop`, always; MCP `browser_file_upload`/`browser_drop` during a run) of secret files: keys, `.env*`, credentials, qa-sdet's data and config.
9. Playwright MCP `browser_*` inside a QA run: the same host, secret, file and budget rules, plus no tab switching and no `browser_close`. Atlassian/Jira MCP, in every session while the plugin is enabled: every tool that isn't a read.

If the guard itself crashes, it denies calls that mention a guarded tool or file and lets everything else through. A command it can't check within 8 seconds (thousands of nested shells or brace groups) is denied: split it.

## Privacy

- **Login:** your QA login lives in the browser profile `~/.local/share/qa-sdet/profiles/main` (mode 700). Only the browser reads it; the guard blocks every other read. It lasts as long as the app's SSO session; then run `/qa-sdet:qa-login` again. `qa-mem browser-open` drops `PLAYWRIGHT_MCP_STORAGE_STATE`, so your personal cookies never enter the QA browser. To sign out for good (or reset a broken profile): `playwright-cli -s=qa close` and `playwright-cli -s=qa-login close`, then delete `~/.local/share/qa-sdet/profiles/` in your own terminal (the guard keeps Claude out of that folder); the next run asks for `/qa-sdet:qa-login`. Uninstalling the plugin leaves `~/.local/share/qa-sdet/` (login and memory DB) and `~/.config/qa-sdet/` in place: delete them yourself.
- **Page snapshots:** playwright-cli and the MCP save page text under `<project>/.playwright-cli/` and `.playwright-mcp/` (kept out of git); each skill run deletes the ones older than 7 days.
- **Reports:** ingest scrubs auth headers, cookies, tokens, URL credentials and secret-named values from the run folder's Markdown and text files, and the values of secret-named keys in JSON; repro specs only lose literal tokens, so they stay runnable. Screenshots can't be scrubbed: look before attaching them to a ticket.
- **Source code:** `/qa-sdet:qa-init` reads it locally; secret files are never listed and mappers write `[secret]` instead of values.
- **Playwright MCP fallback:** it runs with its own settings and cookies. Run it with `--isolated` (the login stays in memory until Claude Code restarts); without it the MCP keeps its login in its own profile (`~/.cache/ms-playwright-mcp/` on Linux), which you delete to sign it out; never attach it to your everyday browser (`--cdp-endpoint`, `--extension`).

Limits: the guard checks command text, not intent. It stops mistakes; it isn't a sandbox. Page JavaScript that builds a URL or a storage name from pieces at run time (`'https:/' + '/evil.example'`, `window['local' + 'Storage']`) passes, and so (in your main session only) does `playwright-cli` started from inside Python or Node code.

## Self-test

```sh
python3 -m unittest discover -s tests -v                  # qa-mem, guard, bypass corpus, docs
python3 -m http.server 8765 -d tests/fixture &            # fixture app with planted bugs
# needs a config with "qaBaseUrl": "http://localhost:8765" and "localhost" in allowedHosts
claude --plugin-dir . -p "/qa-sdet:qa-explore http://localhost:8765/form.html"
```

## Known limits

- One QA run at a time per machine: runs share the `qa` browser session and the `main` profile (and, per project, the run marker).
- The guard checks command text, not intent (see Safety).
- Jira writes stay yours: the run's reply prints the acli command to file each draft; attach its image and repro spec by hand.
