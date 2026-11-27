# qa-sdet development

- Before you commit: `make check` (lint, typecheck, test, test-runtime, validate, security); `make format` fixes layout. Every target: README "Development".
- The plugin runs on the user's system `python3` (3.9+) with the standard library only. Code in `lib/`, `hooks/` and `bin/` never imports a dev dependency and never relies on `.venv`; `make test-runtime` checks it on 3.9.
- `hooks/guard.py` is the trust root and imports nothing from `lib/` (`make architecture`). Rules lib keeps a copy of (host checks, plugin-log masks) are pinned to the guard's by `LibMirrorsGuard` in `tests/test_guard.py`.
- Imports for annotations only go under a module-level `TYPE_CHECKING = False` / `if TYPE_CHECKING:` block: importing `typing` costs every `qa-mem` call about 5 ms. The JSON shapes agents write are typed in `lib/qa_sdet/contracts.py`; runtime validation stays in `ingest.check_run` and the hooks.
- A test that plants a secret-shaped string needs a reviewed entry in `.gitleaks.toml`.
