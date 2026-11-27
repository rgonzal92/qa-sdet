# qa-sdet development

- Before you commit: `make check` (lint, typecheck, test, test-runtime, validate, security); `make format` fixes layout. Every target: README "Development".
- The plugin runs on the user's system `python3` (3.9+) with the standard library only. Code in `lib/`, `hooks/` and `bin/` never imports a dev dependency and never relies on `.venv`; `make test-runtime` checks it on 3.9.
- `hooks/guard.py` is the trust root and imports nothing from `lib/` (`make architecture`). Rules lib keeps a copy of (host checks, plugin-log masks) are pinned to the guard's by `LibMirrorsGuard` in `tests/test_guard.py`.
- Imports for annotations only go under a module-level `TYPE_CHECKING = False` / `if TYPE_CHECKING:` block: importing `typing` costs every `qa-mem` call about 5 ms. The JSON shapes agents write are typed in `lib/qa_sdet/contracts.py`; runtime validation stays in `ingest.check_run` and the hooks.
- A test that plants a secret-shaped string needs a reviewed entry in `.gitleaks.toml`.

## Module map

`lib/qa_sdet/`, one module per line with the `qa-mem` commands or data it owns. A module's top-level imports come from the lines above it; a module it needs from below is imported inside the function that uses it.

Shared
- `core`: config, the DB connection and schema, helpers every module uses
- `scrub`: redaction of secrets from text, JSON and files
- `md`: text made safe for Markdown
- `contracts`: the typed JSON shapes (type checker only)
- `plugin_log`: `plugin-log`, the per-project event log and its digest

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
- `report`: `report` (REPORT.md, the Jira comment, the run index)
- `runstate`: `plan-find`, `next-action`, `explore-next`
- `inbox`: `inbox`, `prep`, `trace`

Entry points
- `cli`: `qa-mem` argument parsing; it calls the modules above
- `hooks/`: `guard` (PreToolUse rules, entered through `pretooluse`), `posttooluse` (feedback after Bash and Write, the call trace), `denied` (logs Claude Code's own denials)
