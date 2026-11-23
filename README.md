# qa-sdet

A Claude Code plugin for SDET work: test plans, test cases, exploratory bug hunting (with or without a Jira ticket) and an SQLite memory of the app under test that updates itself.

## Commands

| Command | What it does |
|---|---|
| `/qa-sdet:qa-login` | You do SSO + MFA in a headed browser, it saves the session state |
| `/qa-sdet:qa-plan PROJ-123` or `"text"` | `qa-out/PROJ-123/plan.md` + `cases.md` (from text: `qa-out/plan-<slug>/`) |
| `/qa-sdet:qa-explore /route [--faults] [--budget N]` | Storyless exploration; bug drafts + repro specs in `qa-out/<page>/` |
| `/qa-sdet:qa-ticket PROJ-123 [--faults] [--budget N]` | Maps each AC to a page (max 3), tests it (PASS/FAIL/NOT_RUN/QUESTION), then explores around it; budget is per page |
| `/qa-sdet:qa-memory card /route` | What's known: facts, locators, coverage, open bugs |

You can also just ask in plain words ("write test cases for PROJ-123", "explore the closures page").

## Install (per machine)

```sh
git clone <this repo> ~/Projects/qa-sdet        # or copy it
claude plugin marketplace add ~/Projects/qa-sdet
claude plugin install qa-sdet@qa-sdet
```
Needs: Claude Code, Python 3.9+ as `python3` (stdlib only; the hooks run it), `playwright-cli` (`npm i -g @playwright/cli`), and optionally `acli` signed in (`acli jira auth login`).

Fallbacks, tried in order:
- **Browser:** `playwright-cli`, then any Playwright MCP you have in Claude Code (tools `mcp__<server>__browser_*`); with neither, runs stop with install steps.
- **Jira:** `acli`, then any Atlassian/Jira MCP (read tools only), then ticket text you paste.

`qa-mem doctor` shows which path each one will take.
With the MCP browser, runs use that browser's own login: the skill asks you to sign in in the MCP's browser window when needed (it must run headed; `qa-login` needs playwright-cli), and with `--isolated` the login lasts until Claude Code restarts. Those runs make no recordings (repro specs are written from the steps), and fault injection only works if the MCP exposes route/offline tools (e.g. `--caps=network`).

Then check the machine: `python3 ~/Projects/qa-sdet/bin/qa-mem doctor` lists what works and the fix for each gap.

After pulling or editing the repo, refresh the installed copy (it is cached):
`claude plugin marketplace update qa-sdet && claude plugin update qa-sdet@qa-sdet`, then restart Claude Code.

## Config: `~/.config/qa-sdet/config.json`

The first run asks for these if the file is missing.

```json
{
  "app": "traffic-ui",
  "qaBaseUrl": "https://qa.example.com",
  "allowedHosts": ["qa.example.com", "api.qa.example.com"],
  "ssoHost": "login.microsoftonline.com",
  "jiraProject": "TRAF",
  "mapRoute": "/map",
  "buildSource": {"type": "selector", "value": "footer .version"},
  "browser": "<chrome | msedge | firefox | webkit>"
}
```
`browser` is `chrome`, `msedge` (your installed Chrome or Edge), `firefox` or `webkit` (Playwright's own builds: `playwright-cli install-browser firefox`). Leave it out: the first run lists what's installed (`qa-mem browsers`) and asks you to choose. A Playwright MCP fallback uses its own `--browser` setting; `qa-mem doctor` suggests one that matches your choice. `buildSource.type` is `selector` (text of an element), `url` (a version endpoint) or `header` (a response header). The build is what keeps memory honest: knowledge from an older build is marked stale and re-verified.

## How it saves tokens and time

- **playwright-cli, not the Playwright MCP.** Snapshots go to files; the agent uses `find`/`eval` instead of reading whole trees. One persistent session (`-s=qa`) so the browser isn't relaunched per action.
- **Exploration runs in a subagent** (`qa-explorer`, Sonnet) with a command budget and time limit that the guard hook enforces. Your main session only gets a ≤20-line summary.
- **Page cards.** Before exploring, `qa-mem card <route>` gives ~30 lines: verified facts, cached locators (acted on directly, no snapshot), heuristics already covered on this build (skipped), open bugs (not re-reported).
- **References load on demand** (`references/`), not on every prompt.

## Memory DB

`~/.local/share/qa-sdet/<app>.db` (SQLite, WAL, FTS5). Tables: pages, facts, locators, coverage, bugs, runs. Capped per page at the freshest 100 facts, 60 locators and 400 coverage rows (2000 runs total); bugs are kept.
Agents never write SQL: each run writes `<project>/qa-out/<slug>/run.json` (schema: `references/run-json.md`) and the plugin's `Stop`/`SubagentStop` hook runs `qa-mem ingest-pending` on `qa-out` in the current and project directory. A file is ingested again only when its content changes. Bugs dedup by a hash of route + symptom (or an explicit known hash); repeats bump `seen_count` once per run; a FIXED bug seen again becomes REGRESSED. Statuses: OPEN, FILED, FIXED, INVALID, REGRESSED.
Coverage from a run that couldn't read the build counts for 7 days, then the heuristic is retried.
Manual: `qa-mem bugs`, `qa-mem bug-status <hash> FILED` (a prefix must match exactly one bug), `qa-mem stale <build>`, `qa-mem search "closure AND schedule"`.
Optional shortcut: `ln -s ~/Projects/qa-sdet/bin/qa-mem ~/.local/bin/qa-mem`.

## Safety (hook: `hooks/guard.py`, PreToolUse on every tool; ~40 ms, and most tools pass straight through)

Denied (everything else passes). Only words in command position are checked, so `echo acli create …` or a `grep` near the DB pass.
1. `playwright-cli` navigation (`goto`/`open`/`tab-new`, URLs in `eval`) to hosts not in `allowedHosts` (add `localhost` there only if you test something local); `run-code` (it runs Node and can reach the host; batch inputs with `eval`, which runs in the page), `attach`, and `show --host` on a non-local address
2. `acli` anything except reads (`view`, `search`, `list`, `get`, `auth login|status`, `--help`, `--version`): bug drafts end with the command for you to run
3. During a run (marker `qa-out/.active`, written by the skill, ignored after 2 h): browser commands (playwright-cli or MCP `browser_*`) past the run's command budget or time limit (`close` always works; loops and xargs can't be counted, so they're denied), `route` / `network-state-set` unless the run used `--faults`, and every browser command if the marker is malformed
4. Session secrets in the conversation: `cookie-*`, `localstorage-*`/`sessionstorage-*` reads, `request`/`request-headers`/`response-headers` (full headers, incl. `Set-Cookie`), `tracing-start` (a trace file holds every header), `eval` reading cookie or web-storage values (key names are fine); any read of `~/.config/qa-sdet/qa-state.json`, of the Playwright MCP's browser profiles (`~/.cache/ms-playwright-mcp`), or your `PLAYWRIGHT_MCP_STORAGE_STATE` cookie jar (Bash incl. `<`, globs, `--opt=path`, inline `python -c`/`node -e` code naming it, and its own folder; Read; Grep; from a folder above it only recursive readers: `grep -r`/`rg`, `find -exec`, `tar`/`zip`/`rsync`/`cp -r`); `state-save` anywhere but the session file
5. `rm`/`mv`/`trash`/`find -delete`/`cp`-onto/redirection onto the memory DB directory (or its parents for recursive deletes); `sqlite3` on it unless `-readonly`; playwright-cli files written into it
6. For the explorer subagent only: editing or deleting the run marker, network tools outside the browser (`curl`, `wget`, `nc`, …), and dumping the page's HTML or scripts (it is a black-box tester; `snapshot` shows structure). It also gets only its own tools (Bash, Read, Write, Grep, Glob, the `browser_*` MCP tools): other MCPs you've connected, such as mail or Jira, are denied inside a test run
7. MCP fallbacks get the same rules:
   - Playwright MCP `browser_*`, inside a QA run (the explorer, or while a run marker exists; outside a run it is your own browser): any `url`, or URL anywhere in an evaluate call, outside `allowedHosts` (and non-web schemes such as `javascript:`), `browser_run_code*`, tracing, request headers, cookie/storage list and get tools, `browser_get_config`, reading cookie or storage values, `route`/offline tools without `--faults`, switching tabs (`browser_tabs select`: other tabs hold the sign-in or your own pages), `browser_close` (it would end the login), and file arguments (any field named like file, path or output) that point at the session file, the memory DB, the run marker, or outside the MCP's folder via `..`. Each call counts against the run budget (`browser_close` is free).
   - Atlassian/Jira/Confluence MCP (matched on the server or tool name, so a Jira server named something else entirely isn't recognised), in every session while the plugin is enabled: every tool that isn't a read (`get…`, `search…`, `lookup…`, `list…`, `fetch…`).

If the guard itself crashes or takes too long, it denies calls that mention a guarded tool or file and lets everything else through.

Privacy: ingest scrubs auth headers, cookies, bearer/JWT/GitHub/AWS tokens, URL credentials and secret-named values from the run folder's Markdown and text files, and only the values of secret-named keys in JSON; repro specs only lose literal tokens, so they stay runnable. Screenshots can't be scrubbed: look before attaching them to a ticket. Response bodies the explorer reads appear in the conversation; it is told to leave login/session/token calls unread, but the guard can't tell which request number is which. If `PLAYWRIGHT_MCP_STORAGE_STATE` is set in your Claude settings, a playwright-cli session starts with those cookies; qa-sdet opens its sessions with `env -u PLAYWRIGHT_MCP_STORAGE_STATE` so your personal cookies never enter the QA browser or `qa-state.json`. A Playwright MCP fallback runs with its own settings and cookies, which the plugin can't change: run it with `--isolated` (the profile stays in memory, so the login lasts until Claude Code restarts and nothing on disk holds it). A profile on disk (`--user-data-dir`, `--storage-state`) holds the QA cookies where the guard can only protect the default locations (`~/.cache/ms-playwright-mcp`, `$XDG_CACHE_HOME`); never attach it to your everyday browser (`--cdp-endpoint`, `--extension`).

Limits: this checks command text, not intent. A determined agent could still reach a host via `curl`, a script file, or JS that builds a URL at runtime. It stops mistakes; it isn't a sandbox.

## Self-test

```sh
python3 -m unittest discover -s tests -v                  # qa-mem, guard, bypass corpus
python3 -m http.server 8765 -d tests/fixture &            # fixture app with 2 planted bugs
# needs a config with "qaBaseUrl": "http://localhost:8765" and "localhost" in allowedHosts
claude --plugin-dir . -p "/qa-sdet:qa-explore http://localhost:8765/form.html --budget 40"
```

## Known limits

- One QA run at a time per machine: runs share the `qa` browser session (and, per project, the run marker).
- The guard checks command text, not intent (see Safety).

## Not in v1

PR QA, regression tests written into your repo, release assessment, resume, Jira writes. Planned once these are stable.
