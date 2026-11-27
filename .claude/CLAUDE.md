# qa-sdet development

- Before you commit: `make check` (lint, typecheck, test, test-runtime, validate, security); `make format` fixes layout. Every target: README "Development".
- The plugin runs on the user's system `python3` (3.9+) with the standard library only. Code in `lib/`, `hooks/` and `bin/` never imports a dev dependency and never relies on `.venv`; `make test-runtime` checks it on 3.9.
- `hooks/guard.py` and its `guard_*.py` modules are the trust root and import nothing from `lib/` (`make architecture`). Rules lib keeps a copy of (host checks, plugin-log masks) are pinned to the guard's by `LibMirrorsGuard` in `tests/test_guard.py`.
- Imports for annotations only go under a module-level `TYPE_CHECKING = False` / `if TYPE_CHECKING:` block: importing `typing` costs every `qa-mem` call about 5 ms. The JSON shapes agents write are typed in `lib/qa_sdet/contracts.py`; runtime validation stays in `ingest.check_run` and the hooks.
- Arguments that travel together are a record: a class under `@core.record` (the guard has its own copy, `guard_paths.record`, pinned by `tests/test_record.py`) with one typed, commented field per line. mypy reads it as a dataclass; importing `dataclasses` costs every call about 6 ms. Its module starts with `from __future__ import annotations`, and a function that takes one annotates it (`stamp: Stamp`), so mypy checks every field it reads. `make lint` fails a function with 8 or more parameters.
- A test that plants a secret-shaped string needs a reviewed entry in `.gitleaks.toml`.

## Module map

`lib/qa_sdet/`, one module per line with the `qa-mem` commands or data it owns. A module's top-level imports come from the lines above it; a module it needs from below is imported inside the function that uses it.

Shared
- `core`: config, the DB connection and schema, helpers every module uses
- `scrub`: redaction of secrets from text, JSON and files
- `md`: text made safe for Markdown
- `contracts`: the typed JSON shapes (type checker only)
- `plugin_log`: the per-project event log: its writer, `plugin-log` note, show and clear
- `digest`: `plugin-log export`, the anonymizer

Memory DB
- `codemap`: the stored code map, `code`, `codemap-forget`
- `scan`: the source classifier, `codemap-scan`
- `cards`: `card`, and the page-knowledge helpers the other readers share
- `memory`: `search`, `bugs`, `patterns`, `impact`, `bug-match`, `bug-status`, `bug-sync`, `stale`, questions, facts, the signal baseline
- `gaps`: `gaps`, `blindspots`, `hotspots`, `history`
- `ingest`: `ingest`, `ingest-pending`, `init-status`, `check_run`

Browser
- `session`: `browser-open`, `session-check`, `page-start`, the build read
- `doctor`: `doctor`, `browsers`, `setup-permissions`
- `evidence`: `evidence`, `annotate`

Run folder
- `ops`: `keys`, `rundir`, `marker`, `dispatch`, `prune`, the call trace
- `probe`: `api-probe`
- `crawl`: `crawl`, `discover`
- `results`: plan and run files merged into cases, bugs, questions and the verdict
- `audit`: `audit-bugs`, the explorer's stop check
- `signals`: `signals-triage`, `body-check`
- `report_blocks`: the pieces of REPORT.md (a case, an AC's results, a bug's entry, the checks before posting, notes, history)
- `report_jira`: the Jira comment
- `report`: `report` (REPORT.md assembled section by section, the run index)
- `runstate`: `plan-find`, `next-action`, `explore-next`
- `inbox`: `inbox`, `prep`, `trace`

Entry points
- `usage`: the command table (`COMMANDS`), option parsing, usage errors
- `cli_run`: one handler per command that needs no memory DB (run folders, browser, reports, plugin log)
- `cli_memory`: one handler per command that opens the memory DB
- `cli`: `qa-mem` entry: checks the arguments, routes to a handler; `ingest-pending`, the hooks' command
- `hooks/`, the PreToolUse guard, entered through `pretooluse`; each module imports only the ones before it: `guard_paths` (hosts, protected folders, run marker, hook input), `guard_shell` (shell text parsing), `guard_tools` (playwright-cli, acli, qa-mem and network rules), `guard_files` (what a command reads or writes in the protected folders), `guard_bash` (the check of one Bash command, what is approved at once), `guard_log` (plugin log, call trace), `guard` (the file and MCP tools, the verdict)
- `hooks/`, the others: `posttooluse` (feedback after Bash and Write, the call trace), `denied` (logs Claude Code's own denials)
