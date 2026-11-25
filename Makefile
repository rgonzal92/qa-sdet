# Developer commands (README "Development"). Python tools run from the repo-local .venv (uv); nothing is installed
# globally. CI runs these same targets. The plugin itself never uses any of this: it runs on the system python3.
.NOTPARALLEL:  # one target at a time, readable output (a shell alias may add -j)
.DEFAULT_GOAL := check
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

UV ?= uv
RUN := $(UV) run --locked
BIN := .venv/bin
# Release binaries, checked against these SHA-256 sums (from each release's checksums file, 2026-11-25). Linux x64.
GITLEAKS_VERSION := 8.30.1
GITLEAKS_SHA256 := 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
ACTIONLINT_VERSION := 1.7.12
ACTIONLINT_SHA256 := 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
# make mutation: a bounded trial on the redactor, the host rules and the verdict (a few minutes)
MUTANTS ?= qa_sdet.scrub.* qa_sdet.core.x_url_allowed__* qa_sdet.core.x_allowed_hosts__* qa_sdet.report.x_verdict__*

.PHONY: setup hooks format lint typecheck test test-runtime coverage security validate architecture mutation check

check: lint typecheck test test-runtime validate security ## the routine local gate (CI runs the same pieces)

setup: ## .venv from uv.lock, plus the pinned gitleaks and actionlint
	$(UV) sync --locked
	@$(MAKE) --no-print-directory $(BIN)/gitleaks $(BIN)/actionlint

$(BIN)/gitleaks:
	curl -sSfL -o $@.tar.gz https://github.com/gitleaks/gitleaks/releases/download/v$(GITLEAKS_VERSION)/gitleaks_$(GITLEAKS_VERSION)_linux_x64.tar.gz
	echo "$(GITLEAKS_SHA256)  $@.tar.gz" | sha256sum -c --quiet -
	tar -xzf $@.tar.gz -C $(BIN) gitleaks
	rm -f $@.tar.gz

$(BIN)/actionlint:
	curl -sSfL -o $@.tar.gz https://github.com/rhysd/actionlint/releases/download/v$(ACTIONLINT_VERSION)/actionlint_$(ACTIONLINT_VERSION)_linux_amd64.tar.gz
	echo "$(ACTIONLINT_SHA256)  $@.tar.gz" | sha256sum -c --quiet -
	tar -xzf $@.tar.gz -C $(BIN) actionlint
	rm -f $@.tar.gz

hooks: ## install the Git pre-commit hooks (Git's; the plugin's Claude Code hooks are hooks/)
	$(RUN) pre-commit install

format: ## format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

lint: ## ruff lint and format check, workflow lint
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(BIN)/actionlint

typecheck: ## mypy against the Python 3.9 runtime floor
	$(RUN) mypy lib/qa_sdet hooks bin/qa-mem

test: ## the unittest suite in .venv (property tests included)
	$(RUN) python -m unittest discover -s tests

test-runtime: ## the runtime contract: the same suite on Python 3.9 with the standard library only
	$(UV) run --no-project --python 3.9 python -m unittest discover -s tests

coverage: ## line and branch coverage, hooks and bin/qa-mem subprocesses included; writes coverage.xml
	$(RUN) coverage erase
	$(RUN) coverage run -m unittest discover -s tests
	$(RUN) coverage combine -q
	$(RUN) coverage report
	$(RUN) coverage xml -q

security: ## secrets in the full history and the working tree; known vulnerabilities in the dev dependencies
	$(BIN)/gitleaks git --redact --no-banner --config .gitleaks.toml .
	$(BIN)/gitleaks dir --redact --no-banner --config .gitleaks.toml .
	$(UV) export --locked --format requirements.txt --all-groups --no-emit-project --quiet -o $(BIN)/../audit-requirements.txt
	$(RUN) pip-audit -r $(BIN)/../audit-requirements.txt --require-hashes --disable-pip --progress-spinner off

validate: ## Claude Code's plugin validator (when claude is on PATH) and the docs-code contracts
	@if command -v claude >/dev/null; then \
		claude plugin validate . && claude plugin validate .claude-plugin/marketplace.json; \
	else echo "validate: claude is not on PATH; skipping claude plugin validate (CI runs it)"; fi
	$(RUN) python -m unittest discover -s tests -p test_docs.py

architecture: ## import boundaries (the guard imports nothing from lib) and lib's copy of the guard's rules
	$(RUN) python -m unittest discover -s tests -p test_guard.py -k ImportBoundaries -k LibMirrorsGuard

mutation: ## bounded mutation trial; mutmut only knows ./, src/, source/, so it runs in a temp copy with lib/ as src/
	$(UV) sync --locked --group mutation
	work=$$(mktemp -d) && echo "mutation work dir: $$work" && \
	git ls-files -co --exclude-standard -z | tar --null -T - -cf - | tar -xf - -C "$$work" && \
	mv "$$work/lib" "$$work/src" && \
	sed -i 's|ROOT / "lib"|ROOT / "src"|' "$$work/tests/support.py" "$$work/tests/test_guard.py" && \
	cd "$$work" && HYPOTHESIS_PROFILE=mutation "$(CURDIR)/$(BIN)/mutmut" run $(foreach m,$(MUTANTS),'$(m)') && \
	"$(CURDIR)/$(BIN)/mutmut" results
