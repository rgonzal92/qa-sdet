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

1. `/qa-sdet:qa-setup`: checks your tools, offers the Pillow install, asks your ticket prefix (e.g. `PROJ`), and adds allow rules for auto mode.
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

Tickets: bare numbers use your `jiraKeyPrefix` (`1234` → `PROJ-1234`); full keys (`OPS-7`) work too; several at once, separated by spaces or commas.
You can also ask in plain words ("write test cases for 1234", "explore the closures page").

The skills run `qa-mem` bare: the plugin's `bin/` folder is on the Bash PATH while the plugin is enabled. If the shell says `qa-mem: command not found`, run `python3 <plugin folder>/bin/qa-mem` instead.

### What qa-ticket does
1. **Analyze** each ticket: summary, numbered acceptance criteria (AC), related open Jira bugs.
2. **Plan** per ticket: scope, approach, risks, out of scope, test data.
3. **Test cases** TC-1..n, each tied to an AC, with steps and the expected result. The report is written now (every case "not run"); `--review` stops here for you to check; reply `go`, or run the same `/qa-sdet:qa-ticket` later without `--review` (your edits to the plan are kept).
4. **Test** with playwright-cli: one explorer per page, one after another.
5. **Track** a status per case: PASS, FAIL, BLOCKED, NOT_RUN or QUESTION, with what was seen and the bug it found.
6. **Report**: `REPORT.md`, re-rendered after every page.
7. **Verdict** per ticket: ✅ PASS · ⚠️ PASS WITH NOTES (minor or suspected bugs, open questions, or an AC with no test case) · ❌ FAIL (a failed case or a HIGH/CRITICAL confirmed bug) · ⛔ BLOCKED (cases blocked or not run, or no cases) · ⏳ NOT STARTED (plan written, no test run yet, e.g. after `--review`). Exact rules: Reading REPORT.md, below.

### Budget (optional)
Runs go until the job is done. `--budget N` (browser commands) and `--minutes M` (at most 480) set a limit per page (for `qa-init --crawl`, on the whole crawl); the guard hook enforces it.

## Output

```
qa-out/
  README.md                       index of all runs, newest first: date, run, verdicts, bugs
  INBOX.md                        from qa-inbox
  2026-11-23-PROJ-1234+PROJ-1250/
    REPORT.md                     open this: verdict, what needs action, test cases with images, not tested
    bugs/BUG-1.md                 Jira-ready bug drafts (the run's reply gives the acli command to file each)
    evidence/BUG-1.png            bug images, problem boxed in red with a label
    evidence/TC-1.png             one image per checked case
    jira-comment-PROJ-1234.md   pass comment draft (PASS and PASS WITH NOTES): paste it into Jira yourself
    repro/BUG-1.spec.ts           Playwright spec that fails while the bug exists
    .data/                        machine files: plan.json, run-<page>.json, snapshots
```
Each run gets a fresh folder. `qa-out/` and `.playwright-cli/` are kept out of git (via `.git/info/exclude`).

## Reading REPORT.md

`qa-mem report` writes it from the plan and the run files; the agents never edit it. Top to bottom:
- **Header and verdict.** One ticket: a verdict line with counts (bugs, open questions, cases that need attention, evidence images) and a link to the Jira pass-comment draft when one exists. Two or more tickets: a summary table (ticket, verdict, why, cases pass/fail/other, bugs).
- **Needs your action:** bugs to file (draft and image links), open questions (blocking first; a question on several pages is one line), failed, blocked and not-run cases. Nothing listed: "Nothing needs your action."
- **Per ticket:** the acceptance criteria with their status, then the test case table (ID, priority, AC, page, case, status, actual, evidence link). Steps, expected, actual and notes sit in one folded block per case (non-pass cases open by default), with the case image `evidence/TC-<n>.png` when it exists. The test plan is a folded block after the cases.
- **Not tested:** one folded block per category (safety rules, out of scope, not reached, needs setup, heuristics, other), each with its count. **Resolved questions** and **Exploration notes** (per page, with heuristic coverage in the title: ✅ checked, ❌ problem found, ⛔ could not be checked) are folded too. `Problems reading this folder` appears when some file could not be read.
- A plan-only report (no run yet) shows the summary table and the questions, and no action block.

**Verdict per ticket** (decided by the renderer, first match wins):
1. ❌ FAIL: any case FAIL, or a CONFIRMED bug with severity CRITICAL or HIGH.
2. ⛔ BLOCKED: any case BLOCKED or not run, or no cases at all.
3. ⚠️ PASS WITH NOTES: any QUESTION case, any other bug (MEDIUM or LOW, or SUSPECTED), or an AC with no test case.
4. ✅ PASS: everything else.

Before any run file exists, every ticket shows ⏳ NOT STARTED. A ticket's bugs are those with its `ticket`, those named by its cases, and (without a ticket field) those found on its cases' routes; an untagged bug that a case of another ticket names belongs to that ticket only.

**Jira pass comment:** `jira-comment-<KEY>.md`, written next to REPORT.md for each ticket whose verdict is PASS or PASS WITH NOTES. It is plain text: a CHECK block (not for Jira: build, images to paste, placeholders), then "Passed on <release names>", "Fixed: <note>" and one `TC-n - RESULT: title` line per case. Release names come from the config key `releaseTemplates` (`{version}` is the ticket's `fix_version`: 3.5 becomes 3.5.0), the note from `fixed_note` in plan.json. The renderer never posts to Jira; a missing name or note stays a visible placeholder. Remove the CHECK block before pasting. Every render rewrites the draft (and removes it once the verdict is no longer a pass), so fill placeholders in Jira or in a copy.

Project safety rules (optional): write them in `<project>/qa-rules.md`, one per line ("never click Confirm", "use only test device X"). Every explorer gets them as hard limits; a check they hold back is listed under Not tested, Safety rules.

## Config: `~/.config/qa-sdet/config.json`

The first run (or `/qa-sdet:qa-setup`) asks for these if the file is missing.

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
- `jiraKeyPrefix`: what bare ticket numbers expand to.
- `browser`: `chrome`, `msedge` (your installed Chrome or Edge), `firefox` or `webkit` (Playwright's own builds: `playwright-cli install-browser firefox`). Leave it out: the first run lists what's installed (`qa-mem browsers`) and asks you to choose.
- `releaseTemplates` (optional): release names for the Jira pass-comment draft, such as `["Release-A-{version}", "Release-B-{version}"]`. Local config only, never in the repo.
- `buildSource.type`: `selector` (text of an element), `url` (a version endpoint) or `header` (a response header). The build keeps memory honest: knowledge from an older build is marked stale and re-verified.

`qa-mem doctor` lists what works and the fix for each gap (`FIX <what>  -> <fix>`). `/qa-sdet:qa-setup` runs it. Other skills run it only after they write or change the config, or when a tool fails; they ask you before applying a config fix it prints (a `qaBaseUrl` without `https://`, an `ssoHost` given as a URL, a `jiraKeyPrefix` like `PROJ-1234`), and a run stops while one stays open.

## Fallbacks
- **Jira:** `acli`, else ticket text you paste. There is no Jira MCP path.
- **Browser:** `playwright-cli` only. Without it, runs stop with the install command.

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
Calls these rules match skip the prompt and the classifier, also when the explorer or a mapper makes them. Auto mode drops broad rules: rules for agents and for interpreters with wildcards (`Bash(python3 *)`), which is why the skills run the plugin's `qa-mem` command bare (the plugin's `bin/` folder is on the Bash PATH), and why starting an explorer or mapper still goes past the classifier. The guard also approves the calls it fully checked itself (Safety below). When something is still blocked, `/permissions` → Recently blocked and `<project>/qa-out/guard-denials.log` say which layer did it. If auto mode blocks the `--apply` step itself (it edits Claude Code's own settings), type `! qa-mem setup-permissions --apply` in the prompt to run it as your own command. The plugin's guard hook still runs on every call and blocks the unsafe ones (Safety below). Restart Claude Code afterwards. If your company manages settings centrally, ask IT to add the rules there.

## Models
Both agents (the explorer and the `qa-init` code mapper) run on Haiku with high effort. In a side-by-side fixture run, Haiku found the same planted bugs as Sonnet at about 1/12 of the explorer cost. To switch back, set `model: sonnet` in `agents/qa-explorer.md`.

## Memory DB

`~/.local/share/qa-sdet/<app>.db` (SQLite, WAL, FTS5). Tables: pages, facts, locators, coverage, bugs, runs, codemap. Capped per page at the freshest 100 facts, 60 locators and 400 coverage rows (2000 runs total); bugs are kept.
Agents never write SQL: each explored page writes `qa-out/<run>/.data/run-<page>.json` (schema: `references/run-json.md`) and the plugin's `Stop`/`SubagentStop` hook ingests it. A file is ingested again only when its content changes. Bugs dedup by a hash of route + symptom; repeats bump `seen_count` once per run; a FIXED bug seen again becomes REGRESSED. Statuses: OPEN, FILED, FIXED, INVALID, REGRESSED.
Coverage from a run that couldn't read the build counts for 7 days, then the heuristic is retried.
Code map (`/qa-sdet:qa-init`): `qa-mem codemap-scan` lists the source files deterministically (skipping `node_modules`, build output, lockfiles and minified bundles), one `qa-mapper` agent per area reads only those (a large area is split into chunks of about 40 files / 60k tokens, up to 6 mappers at a time), and the hook ingests their `qa-out/init/code-map-<area>[-<n>].json` into the `codemap` table. In a git repo only tracked and not-ignored files are read; links, very large files and secret stores (`.env*`, keys and certificates, `secrets.*`/`credentials.*`/`config.*` files, config modules holding credentials) never are. Re-runs read only files whose content changed (the whole frontend and tests when a router changed; a new file-routed page that reuses unchanged components gets their locators with `--full`, and so does a page renamed inside its own one-route file: its old rows stay until then). Code is never an oracle: a page that differs from the code is a question for you.
Manual: `qa-mem bugs`, `qa-mem bug-status <hash> FILED` (a prefix must match exactly one bug), `qa-mem stale <build>`, `qa-mem search "closure schedule"` (every word must match; `-word` excludes).

## Safety (hook: `hooks/guard.py`, PreToolUse on every tool)

The guard stops mistakes; it isn't a sandbox. It reads the command text without emulating the shell and denies these (everything else passes, so `echo acli create …` or a `grep` near the DB pass):
1. Jira writes: `acli` anything except reads (`view`, `search`, `list`, `get`, `auth login|status`, `--help`, `--version`), an acli command with a flag before its command words (`acli --profile p jira …`; write `acli jira … --profile p`), acli run from inline code, and every Atlassian/Jira MCP tool that isn't a read. Bug drafts end with the command for you to run.
2. Other hosts and browsers: `playwright-cli` navigation (`goto`/`open`/`tab-new`, `qa-mem browser-open`, URLs in `eval`) to hosts not in `allowedHosts` (add `localhost` there only if you test something local); another browser or config (`--config`, `--extension`, `--cdp`, `--endpoint`, `attach`, `PLAYWRIGHT_MCP_*`/`PWTEST_*` variables); `show --host` on a non-local address; `--profile`/`--user-data-dir` with anything but the main profile on `open`.
3. Session secrets: `cookie-*`, `localstorage-*`/`sessionstorage-*` reads, full request/response headers, `tracing-start`, `state-save` (it dumps cookies), `run-code` (it runs Node), `delete-data` (it wipes the signed-in profile), `eval` reading cookie or web-storage values (lengths, key names and the word "cookie" in page text are fine); any use of the browser profile folder `~/.local/share/qa-sdet/profiles/` other than `qa-mem browser-open`; `playwright-cli upload`/`drop` of a secret file (keys, `.env*`, credentials, qa-sdet's data and config) or of a glob.
4. qa-explorer only: changing the qa-sdet config (it holds `allowedHosts`) or `.playwright/cli.config.json`; network tools except HTTP clients (`curl`, `wget`, …) to the allowed hosts, without proxies or option files; running a script file with a shell or interpreter (`bash x.sh`, `python3 x.py`, `source x`; `qa-mem` excepted), because a page it read could steer it into writing one. Otherwise it has the main session's rules: it cleans up, runs `qa-mem`, writes and deletes files.
5. The memory DB folder `~/.local/share/qa-sdet/`: deleting, moving, overwriting or unpacking into it, or removing a folder above it; `sqlite3` on it unless `-readonly`.
6. Run limits (marker `qa-out/.active`, written by `qa-mem marker start`; ignored after 8 h or when malformed): browser commands past a budget or time limit you set (`close` always works), and playwright-cli in a loop when the run has a budget. Without a marker nothing is counted or limited, for the explorer too. `route` / `network-state-set` (fault injection) are a separate rule: denied unless the run started with `--faults`, so always denied with no marker.
7. qa-mapper: only reading tools, and writing only its `qa-out/init/code-map-<area>[-<n>].json`; no reads of secret files (`.env*`, keys and certificates, `secrets.*`/`credentials.*` files, `~/.ssh`, `~/.aws` and other credential folders, qa-sdet's data and config).
8. Calls it fully checked in one pass (only playwright-cli, `qa-mem` other than `qa-mem setup-permissions --apply`, and `head`/`tail`/`grep` filters, joined by `;` `&&` `||` `|`, writing only into `qa-out`) it approves itself, so they skip Claude Code's prompt. Every denial, the guard's and the auto-mode classifier's (a `PermissionDenied` hook, for calls that concern qa-sdet), is logged to `<project>/qa-out/guard-denials.log`: time, layer, agent, reason and the call, secrets masked, newest 1000 lines.

Run guarded commands directly: when a call names `playwright-cli`, `acli`, `qa-mem` or the data folder and hides what runs (a program or verb from `$VAR` or `$(...)`, `eval "$X"`, `source` or a script piped to a shell, inline Python or Node code, `xargs`, a command string handed to `ssh`, `watch`, `trap` and the like, a computed path near the data folder, nesting deeper than 4), the guard denies it rather than guess; spell out program, verb and paths.

If the guard itself crashes, it denies calls that mention a guarded tool or path (any call of the mapper) and lets everything else through. A command over 100 KB that names one is denied: split it.

Known limits: it checks command text, not intent. It doesn't read scripts on disk (`bash build.sh` runs whatever that file says), obfuscated text passes (`$'\x70…'`, names split across variables, brace paths), the mapper may content-search a folder that holds `.env` files, and a Playwright MCP browser isn't guarded at all, so don't install one for qa-sdet.

## Privacy

- **Login:** your QA login lives in the browser profile `~/.local/share/qa-sdet/profiles/main` (mode 700). Only the browser reads it; the guard blocks every other read. It lasts as long as the app's SSO session; then run `/qa-sdet:qa-login` again. `qa-mem browser-open` drops every `PLAYWRIGHT_MCP_*` and `PWTEST_*` variable (such as `PLAYWRIGHT_MCP_STORAGE_STATE`), so your personal cookies never enter the QA browser, and the guard denies setting one on a `playwright-cli` call. To sign out for good (or reset a broken profile): `playwright-cli -s=qa close` and `playwright-cli -s=qa-login close`, then delete `~/.local/share/qa-sdet/profiles/` in your own terminal (the guard keeps Claude out of that folder); the next run asks for `/qa-sdet:qa-login`. Uninstalling the plugin leaves `~/.local/share/qa-sdet/` (login and memory DB) and `~/.config/qa-sdet/` in place: delete them yourself.
- **Page snapshots:** playwright-cli saves page text under `<project>/.playwright-cli/` (kept out of git); each skill run deletes the ones older than 7 days.
- **Reports:** ingest scrubs auth and cookie header values, well-known token formats (JWT, GitHub, Slack, AWS, GitLab, Atlassian, `sk-` keys, private keys, `Bearer` tokens), URL credentials and token query parameters from the run folder's Markdown and text files, and the values of secret-named keys in JSON (a password written out in prose is not recognised: keep real passwords out of notes); repro specs only lose literal tokens, so they stay runnable. Screenshots can't be scrubbed: look before attaching them to a ticket.
- **Source code:** `/qa-sdet:qa-init` reads it locally; secret files are never listed and mappers write `[secret]` instead of values.

Limits: see Known limits under Safety. In short, the guard checks command text, not intent, and page JavaScript that builds a URL or a storage name from pieces at run time (`'https:/' + '/evil.example'`, `window['local' + 'Storage']`) passes.

## Self-test

```sh
python3 -m unittest discover -s tests -v                  # qa-mem, code map, guard, report, docs
python3 -m http.server 8765 -d tests/fixture &            # fixture app with planted bugs
# needs a config with "qaBaseUrl": "http://localhost:8765" and "localhost" in allowedHosts
claude --plugin-dir . -p "/qa-sdet:qa-explore http://localhost:8765/form.html"
```

## Known limits

- One QA run at a time per machine: runs share the `qa` browser session and the `main` profile (and, per project, the run marker).
- The guard checks command text, not intent (see Safety).
- Jira writes stay yours: the run's reply prints the acli command to file each draft; attach its image and repro spec by hand.
