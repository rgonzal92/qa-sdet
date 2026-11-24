"""The instruction files and the code must agree; drift here makes agents fail quietly."""
import re
import shutil
import subprocess
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
qm = SourceFileLoader("qa_mem_docs", str(ROOT / "bin/qa-mem")).load_module()
DOCS = sorted([*ROOT.glob("skills/*/SKILL.md"), *ROOT.glob("agents/*.md"), *ROOT.glob("references/*.md")])


class Docs(unittest.TestCase):
    def test_heuristic_ids_match_code(self):
        table = (ROOT / "references/heuristics.md").read_text()
        ids = re.findall(r"^\| ([a-z][a-z0-9-]+) \|", table, re.M)
        self.assertEqual(ids, qm.HEURISTICS)

    def test_common_steps_referenced_exist(self):
        defined = set(re.findall(r"^## (C\d+)\.", (ROOT / "references/common.md").read_text(), re.M))
        for f in DOCS:
            for step in re.findall(r"\bC(\d+)\b", f.read_text()):
                self.assertIn(f"C{step}", defined, f"{f.name} refers to C{step}")

    def test_referenced_plugin_files_exist(self):
        for f in DOCS:
            for rel in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/\.\./\.\./([\w./-]+)", f.read_text()):
                self.assertTrue((ROOT / rel).exists(), f"{f.name}: {rel}")
            for name in re.findall(r"`((?:[\w-]+)\.md)`", f.read_text()):
                if name not in ("REPORT.md", "INBOX.md", "snapshot.md"):  # run outputs, not plugin files
                    self.assertTrue((ROOT / "references" / name).exists() or (ROOT / name).exists(), f"{f.name}: {name}")

    def test_no_hardcoded_browser(self):
        """The browser is the user's choice (config `browser`, asked in C1), never assumed."""
        for f in DOCS + [ROOT / "README.md"]:
            text = f.read_text()
            self.assertNotRegex(text, r"--browser\s*[= ]\s*(chrome|chromium|msedge|firefox|webkit)\b", f.name)
            self.assertNotRegex(text, r"(?i)\b(firefox|chrome|chromium|webkit|(ms)?edge) headless\b", f.name)  # bug template text
            self.assertNotRegex(text, r'"browser":\s*"(chrome|msedge|firefox|webkit)"', f.name)  # a preset config value

    def test_text_plans_are_not_offered_to_qa_ticket(self):
        """A plan from text uses key FEATURE, which qa-ticket's `qa-mem keys` rejects: qa-plan must not offer it."""
        with self.assertRaises(qm.BadInput):
            qm.expand_keys(["FEATURE"], "STUDIO")
        reply = re.search(r"^8\. .*", (ROOT / "skills/qa-plan/SKILL.md").read_text(), re.M).group(0)
        self.assertIn("plan from text", reply)

    def test_profile_in_use_is_handled(self):
        """Sessions qa and qa-login share the main profile, which opens in one browser only (playwright-cli: `Browser
        is already in use`; `open` only replaces a session of the same name). qa-login must close a leftover qa session
        first, and the explorer must not report a locked profile as `browser tool failed` (that switches to the MCP)."""
        a, b = (qm.browser_cmd(s, False, None, "chrome", "/p") for s in ("qa", "qa-login"))
        self.assertEqual(a[a.index("--profile") + 1], b[b.index("--profile") + 1])
        login = (ROOT / "skills/qa-login/SKILL.md").read_text()
        step2 = re.search(r"^2\. .*", login, re.M).group(0)
        self.assertLess(step2.index("playwright-cli -s=qa close"), step2.index("qa-mem browser-open --session qa-login"))
        self.assertIn("already in use", step2)
        start = re.search(r"^1\. \*\*Start:\*\*.*", (ROOT / "agents/qa-explorer.md").read_text(), re.M).group(0)
        self.assertLess(start.index("already in use"), start.index("browser tool failed"))
        self.assertIn("browser profile in use", (ROOT / "references/common.md").read_text())

    def test_statuses_in_docs_are_real(self):
        text = (ROOT / "skills/qa-memory/SKILL.md").read_text()
        for status in re.findall(r"\b(OPEN|FILED|FIXED|INVALID|REGRESSED)\b", text):
            self.assertIn(status, qm.STATUSES)

    def test_report_md_states_the_renderer_verdict_rules(self):
        """report.md is what skills tell users about verdicts: it must name the rules the renderer applies."""
        doc = (ROOT / "references/report.md").read_text()
        t = {"key": "AB-1", "acs": [{"id": "AC-1"}, {"id": "AC-2"}]}
        self.assertEqual(qm.verdict(t, [{"id": "TC-1", "ticket": "AB-1", "ac": "AC-1", "status": "PASS"}], [])[0],
                         "PASS WITH NOTES")
        self.assertIn("an AC with no test case", re.search(r"^3\. .*", doc, re.M).group(0))
        cases = [{"id": "TC-1", "ticket": "A-1", "route": "/f", "status": "FAIL", "bug": "BUG-1"},
                 {"id": "TC-2", "ticket": "B-1", "route": "/f", "status": "PASS"}]
        self.assertEqual(qm.ticket_bugs("B-1", cases, [{"id": "BUG-1", "route": "/f"}]), [])
        self.assertIn("belongs to that ticket only", doc)

    def test_minutes_cap_is_documented(self):
        with self.assertRaises(qm.BadInput):
            qm._count({"--minutes": str(qm.MARKER_HOURS * 60 + 1)}, "--minutes", qm.MARKER_HOURS * 60)
        self.assertIn(f"at most {qm.MARKER_HOURS * 60}", (ROOT / "references/common.md").read_text())

    def test_code_map_paths_reject_the_whole_file(self):
        """ingest_codemap rejects a map with any row outside the scan; the docs must not say such rows are dropped
        (a mapper would then leave them in), and a mapper must fix only its own out after a hook error."""
        for f in ("references/code-map.md", "agents/qa-mapper.md"):
            text = (ROOT / f).read_text()
            self.assertNotRegex(text, r"rows from other files are dropped|dropped at ingest", f)
        self.assertIn("leave them alone", (ROOT / "agents/qa-mapper.md").read_text())

    def test_map_mode_locators_match_code_rows(self):
        """A crawl confirms a code locator only when the run JSON locator equals the code row's value exactly."""
        text = (ROOT / "agents/qa-explorer.md").read_text()
        self.assertIn('{"name": <its label>, "locator": <its value>}', text)
        self.assertIn("`[locator]` lines from `qa-mem code", (ROOT / "skills/qa-init/SKILL.md").read_text())


    def test_readme_lists_every_verdict(self):
        """README is the user's key to the Summary table: every verdict the renderer prints must be explained there."""
        text = (ROOT / "README.md").read_text()
        for icon in qm.VERDICT_ICON.values():
            self.assertIn(icon, text)

    def test_no_old_layout_run_json_or_mapper_path(self):
        """Run JSONs live in <run>/.data/run-<slug>.json; mappers may write code-map-<area>-<n>.json chunks."""
        for f in DOCS + [ROOT / "README.md"]:
            self.assertNotRegex(f.read_text(), r"qa-out/\*\*/run\.json|in run\.json `coverage`", f.name)
        self.assertNotIn("code-map-<area>.json", (ROOT / "README.md").read_text())

    def test_readme_permission_rules_match_code(self):
        block = (ROOT / "README.md").read_text().split("adds these allow rules", 1)[1].split("```", 2)[1]
        self.assertEqual(block.strip().splitlines(), qm.PERMISSION_RULES)

    def test_readme_safety_and_privacy_match_guard(self):
        """r12/r19, loosened: the guard's rules (no limits without a marker, the explorer's few extra ones, secret
        uploads, mapper secret reads, the denial log) and how to delete the persistent login must reach the user."""
        text = (ROOT / "README.md").read_text()
        safety = text.split("## Safety", 1)[1].split("## Privacy", 1)[0]
        for s in ("Without a marker nothing is counted or limited", "changing the qa-sdet config", "browser_file_upload",
                  "`playwright-cli upload`", "no reads or content searches of secret files", "`.env*`",
                  "guard-denials.log", "approves itself"):
            self.assertIn(s, safety)
        privacy = text.split("## Privacy", 1)[1].split("## Self-test", 1)[0]
        self.assertIn("then delete `~/.local/share/qa-sdet/profiles/` in your own terminal", privacy)

    def test_no_default_run_limit(self):
        """User decision: runs go until the job is done unless the user gives --budget/--minutes."""
        for f in DOCS + [ROOT / "README.md"]:
            text = f.read_text()
            self.assertNotRegex(text, r"--budget`? *(=|N =|\d)|--minutes \d|× *routes|\d+ minutes", f.name)
        init = (ROOT / "skills/qa-init/SKILL.md").read_text()
        self.assertIn("--budget N] [--minutes M]", init)
        self.assertNotRegex(init, r"static ones only \(no `:id`\), at most")

    def test_replies_are_ordered_and_point_at_the_report(self):
        """Human-facing replies: result first, the report path, then exactly one next step."""
        sk = lambda n: (ROOT / f"skills/{n}/SKILL.md").read_text()
        for name in ("qa-ticket", "qa-explore", "qa-plan", "qa-init"):
            self.assertRegex(sk(name), r"REPORT\.md`? path[^\n]*one next step", name)
        for name in ("qa-setup", "qa-inbox", "qa-login"):
            self.assertRegex(sk(name), r"one next step|`Next:` line as the last line|Next: /qa-sdet:", name)
        self.assertIn("NOT STARTED", sk("qa-ticket"))
        self.assertIn("`--review` run", sk("qa-ticket"))  # a later qa-ticket keeps the reviewed plan
        login = sk("qa-login")
        self.assertLess(login.index("`playwright-cli -s=qa close` (on both outcomes)"), login.index("qa-mem login-ok"))

    def test_case_status_rules_are_decidable(self):
        """r17: haiku marked a page showing '0 closures' after a 404 as BLOCKED (step 5 allowed "data, environment"),
        and sonnet used NOT_RUN for "not testable". Each status needs one rule, a failing app call is never env trouble."""
        ex = (ROOT / "agents/qa-explorer.md").read_text()
        step5 = ex[ex.index("5. **Cases"):ex.index("6. **Map page")]
        order = [step5.index(f"   - {s}:") for s in ("FAIL", "BLOCKED", "QUESTION", "PASS", "NOT_RUN")]
        self.assertEqual(order, sorted(order))  # FAIL wins over a partly blocked case
        self.assertIn("NOT_RUN: only when the budget or time ran out", step5)
        self.assertIn("A failing app request is app behaviour, never BLOCKED or ENV_FAILURE", step5)
        self.assertIn("they never excuse a failure the app produces on its own", step5)
        self.assertNotIn("data, environment, login", step5)
        self.assertIn("a `not_tested` line saying why", step5)  # the loop's end is checkable without a budget
        self.assertIn("An app request that fails while the page works is product behaviour",
                      (ROOT / "references/severity.md").read_text())
        rj = (ROOT / "references/run-json.md").read_text()
        self.assertIn("one you reached but couldn't do is `BLOCKED`", rj)
        self.assertIn("a KNOWN BUG's hash, or a known Jira key", rj)

    def test_case_fields_are_specified_for_rerunnable_cases(self):
        """r21: planned cases bundled two inputs in one case, used `if … otherwise note it` steps that needed --faults
        (a NOT_RUN that blocked the ticket), named AC-2 checks under ac AC-1, and listed "API errors" as out of scope,
        which the explorer read as an excuse. The multi-AC form the docs give must be one the renderer splits."""
        tpl = (ROOT / "references/plan-template.md").read_text()
        for rule in ("an id never changes", "`AC-1, AC-2`", "exactly `P1`, `P2` or `P3`", "One input value per case",
                     "No `if … otherwise` branches", "never from code", "never what it judges", "(needs --faults)"):
            self.assertIn(rule, tpl)
        rep = (ROOT / "references/report.md").read_text()
        self.assertIn('`"AC-1, AC-2"`', rep)
        self.assertIn("are unique (the report shows only the first case with an id)", rep)
        self.assertEqual(qm._case_acs({"ac": "AC-1, AC-2"}), {"AC-1", "AC-2"})
        merged = qm.merged_cases({"cases": [{"id": "TC-1", "ac": ["AC-1", "AC-2"]}]}, [])
        self.assertEqual(qm._case_acs(merged[0]), {"AC-1", "AC-2"})
        tk = (ROOT / "skills/qa-ticket/SKILL.md").read_text()
        self.assertIn("keep every id the user left", tk)
        self.assertIn("with `--faults`, an AC's API error and empty-response cases are cases instead", tk)

    def test_bug_drafts_are_jira_ready_and_the_filing_command_is_safe(self):
        """r23 (cmp drafts): the filing line put the title in double quotes, so a title quoting page text ran
        `$(…)`/backticks in the user's shell; the line sat inside the draft, so the Jira description got the command and
        the local home path; steps said `Open /form.html` with no environment; haiku copied `(or the MCP's browser)`
        literally; no severity reason; specs needed a Playwright config with baseURL; a screenshot showed the recorder
        toolbar; a label sat by the wrong box."""
        import subprocess
        tpl = (ROOT / "references/bug-template.md").read_text()
        self.assertNotIn("acli", tpl.split("---", 1)[1])  # the file becomes the Jira description
        self.assertNotIn("or the MCP's browser", tpl)
        for part in ("**Env:** <qaBaseUrl>", "1. Open <qaBaseUrl>/", "## Impact", "the severity.md line",
                     "No secrets or personal data", "no `'` character", "attach these files to the Jira item",
                     "BASE_URL=<qaBaseUrl> npx playwright test"):
            self.assertIn(part, tpl)
        common = (ROOT / "references/common.md").read_text()
        cmd = re.search(r"`(acli jira workitem create [^`]+)`", common).group(1)
        self.assertIn("--summary '<the draft's # title>'", cmd)
        self.assertIn("--description-file '<absolute draft path>'", cmd)
        title = "Status shows `id` and $(echo INJECTED) after Save"  # a title quoting page text, without '
        line = cmd.replace("acli ", "printf '%s\\n' ", 1).replace("<the draft's # title>", title) \
            .replace("<absolute draft path>", "/p/bugs/BUG-1.md").replace("<PROJ>", "DEMO")
        out = subprocess.run(["bash", "-c", line], capture_output=True, text=True).stdout
        self.assertIn(title + "\n", out)
        self.assertNotIn("INJECTED after", out.replace(title, ""))
        for skill in ("qa-explore", "qa-ticket"):
            self.assertIn("the C8 filing command", (ROOT / f"skills/{skill}/SKILL.md").read_text())
        ex = (ROOT / "agents/qa-explorer.md").read_text()
        self.assertNotIn("acli comment line", ex)
        self.assertIn("test.use({ baseURL: process.env.BASE_URL ?? '<qaBaseUrl>' });", ex)
        self.assertIn("after `recording-stop`", ex)
        self.assertIn("the first box is the element the label describes", ex)


    def test_doctor_config_fix_lines_are_acted_on(self):
        """r22 added doctor FIX lines for a bad qaBaseUrl scheme, ssoHost as a URL and a bad jiraKeyPrefix; C1 told the
        agent to fix only config FIX lines after writing a NEW config, and qa-setup only asked for a missing prefix."""
        common = (ROOT / "references/common.md").read_text()
        c1 = common[common.index("## C1."):common.index("## C2.")]
        self.assertIn("also when the config already existed", c1)
        prefixes = re.findall(r"`(\w+)`", c1[c1.index("Act on every `FIX` line"):].split(";")[0])
        import json, os, subprocess, sys, tempfile
        good = dict(app="x", qaBaseUrl="https://qa.example.com", allowedHosts=["qa.example.com"], ssoHost="login.x.com",
                    buildSource={"type": "selector", "value": "footer"}, browser="chrome")
        bad = [dict(qaBaseUrl="qa.example.com"), dict(qaBaseUrl="https://other.example.com"), dict(ssoHost="https://l.x/"),
               dict(ssoHost="qa.example.com"), dict(jiraKeyPrefix="STUDIO-1234"), dict(allowedHosts="qa.example.com"),
               dict(buildSource="footer"), dict(app=""), dict(browser="opera"), dict(browser=None)]
        tools = ("python", "playwright-cli", "fallback browser", "browser ", "acli", "Pillow", "signed-in", "memory DB")
        seen = set()
        with tempfile.TemporaryDirectory() as d:
            env = dict(PATH=os.path.dirname(sys.executable), HOME=d, QA_SDET_CONFIG=f"{d}/config.json",
                       QA_SDET_DATA=f"{d}/data")
            for cfg in [*bad, "[1]", "{bad"]:
                Path(env["QA_SDET_CONFIG"]).write_text(cfg if isinstance(cfg, str) else json.dumps({**good, **cfg}))
                out = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "doctor"], env=env,
                                     capture_output=True, text=True, timeout=60).stdout
                for what in re.findall(r"^FIX  (.*?)(?:  -> |$)", out, re.M):
                    if not what.startswith(tools):
                        seen.add(what)
                        self.assertTrue(what.split()[0] in prefixes or "browser" in what, what)
        self.assertGreaterEqual(len(seen), 9)
        self.assertIn("handle it as C1 says for doctor's FIX lines", (ROOT / "skills/qa-setup/SKILL.md").read_text())
        self.assertRegex((ROOT / "skills/qa-login/SKILL.md").read_text(), r"Then `qa-mem doctor`: a `FIX` line about")

    def test_user_and_page_text_is_quoted_safely(self):
        """Single quotes stop `;` and `$(…)`, but a `'` inside the text ends them: `qa-mem keys '1234'; touch X; ''`."""
        import subprocess, tempfile
        rule = "'\\''"
        for f in ("references/common.md", "agents/qa-explorer.md", "skills/qa-memory/SKILL.md"):
            self.assertIn(rule, (ROOT / f).read_text(), f)
        for s in ("qa-plan", "qa-init"):  # they read only some C-steps: the Quoting rule must be among them
            self.assertIn("its Quoting rule", (ROOT / f"skills/{s}/SKILL.md").read_text(), s)
        for f in DOCS:
            self.assertNotRegex(f.read_text(), r"qa-mem hash <route>", f.name)
        with tempfile.TemporaryDirectory() as d:
            for arg in ("1234; touch PWNED", "$(touch PWNED)", "`touch PWNED`", "1234'; touch PWNED; echo '", "Can't"):
                quoted = "'" + arg.replace("'", rule) + "'"
                out = subprocess.run(["bash", "-c", f"printf %s {quoted}"], cwd=d, capture_output=True, text=True)
                self.assertEqual(out.stdout, arg)
                self.assertFalse((Path(d) / "PWNED").exists(), arg)

    def test_frontmatter_invocation_and_hints(self):
        """Descriptions pick the right skill; hints name only flags the body handles; agent model/effort are valid."""
        def fm(path):
            head = path.read_text().split("---\n")[1]
            return dict(re.findall(r"^([\w-]+): (.*)$", head, re.M))
        for path in ROOT.glob("skills/*/SKILL.md"):
            meta, body = fm(path), path.read_text().split("---\n", 2)[2]
            self.assertEqual(meta["name"], path.parent.name)
            if meta.get("disable-model-invocation") != "true":
                self.assertIn("Use when", meta["description"], path.parent.name)
            for flag in re.findall(r"--[a-z]+", meta.get("argument-hint", "")):
                self.assertIn(flag, body, f"{path.parent.name}: {flag}")
        # setup changes Claude Code's settings: only the user starts it (v0.4 contract); login is a user request
        self.assertEqual(fm(ROOT / "skills/qa-setup/SKILL.md").get("disable-model-invocation"), "true")
        self.assertNotIn("disable-model-invocation", fm(ROOT / "skills/qa-login/SKILL.md"))
        for path in ROOT.glob("agents/*.md"):
            meta = fm(path)
            self.assertIn(meta.get("model"), ("haiku", "sonnet", "opus", "inherit"), path.name)
            self.assertIn(meta.get("effort"), ("low", "medium", "high", "xhigh", "max"), path.name)


# `playwright-cli --help` and `playwright-cli --help <verb>` (0.1.24): every verb and its options
PW_GLOBAL = {"help", "json", "raw", "version"}
PW_VERBS = {k: set(v.split()) for k, v in {
    'attach': 'cdp endpoint extension config session idle-timeout', 'check': '', 'clear-color-scheme': '',
    'clear-contrast': '', 'clear-forced-colors': '', 'clear-media': '', 'clear-reduced-motion': '',
    'click': 'modifiers', 'close': '', 'close-all': '', 'config-print': '', 'console': 'all clear',
    'cookie-clear': '', 'cookie-delete': 'domain path', 'cookie-get': '', 'cookie-list': 'domain path',
    'cookie-set': 'domain path expires httpOnly secure sameSite', 'dblclick': 'modifiers', 'delete-data': '',
    'detach': '', 'dialog-accept': '', 'dialog-dismiss': '', 'drag': '', 'drop': 'path data', 'eval': 'filename',
    'fill': 'submit', 'find': 'regex filename max-results', 'generate-locator': '', 'go-back': '', 'go-forward': '',
    'goto': '', 'highlight': 'hide style', 'hover': '', 'install': 'skills global',
    'install-browser': 'with-deps dry-run list force only-shell no-shell', 'keydown': '', 'keyup': '',
    'kill-all': '', 'list': 'all', 'localstorage-clear': '', 'localstorage-delete': '', 'localstorage-get': '',
    'localstorage-list': '', 'localstorage-set': '', 'mousedown': '', 'mousemove': '', 'mouseup': '',
    'mousewheel': '', 'network-state-set': '',
    'open': 'browser config device headed idle-timeout mobile persistent profile', 'pause-at': '', 'pdf': 'filename',
    'press': '', 'recording-start': '', 'recording-stop': '', 'reload': '', 'request': 'filename',
    'request-body': 'filename', 'request-headers': 'filename', 'requests': 'static filter all clear', 'resize': '',
    'response-body': 'filename', 'response-headers': 'filename', 'resume': '',
    'route': 'status body content-type header remove-header', 'route-list': '', 'run-code': 'filename',
    'screenshot': 'filename type full-page hires', 'select': '', 'sessionstorage-clear': '',
    'sessionstorage-delete': '', 'sessionstorage-get': '', 'sessionstorage-list': '', 'sessionstorage-set': '',
    'set-color-scheme': '', 'set-contrast': '', 'set-forced-colors': '', 'set-media': '', 'set-reduced-motion': '',
    'show': 'port host annotate kill', 'snapshot': 'filename depth boxes', 'state-load': '', 'state-save': '',
    'step-over': '', 'tab-close': '', 'tab-list': '', 'tab-new': 'isolated-context', 'tab-select': '',
    'tracing-start': '', 'tracing-stop': '', 'tray': '', 'type': 'submit', 'uncheck': '', 'unroute': '',
    'upload': '', 'video-chapter': 'description duration', 'video-hide-actions': '',
    'video-show-actions': 'duration position cursor point-style highlight-style title-style',
    'video-start': 'size fps cursor', 'video-stop': '', 'webmcp-call': 'params frame', 'webmcp-list': '',
}.items()}
# browser docs where commands are also written without the `playwright-cli -s=qa` prefix
PW_DOCS = ["README.md", "agents/qa-explorer.md", "references/playwright-cli-tips.md",
           "references/playwright-mcp-tips.md", "references/common.md", "skills/qa-login/SKILL.md"]
PW_NOT_CLI = ("qa-", "data-", "aria-", "python-", "incidents-", "bug-status", "playwright-cli")


def pw_commands():
    """(file, span, verb, tokens after the verb) for every playwright-cli command the docs spell out."""
    for path in [ROOT / "README.md", *DOCS]:
        rel, text = str(path.relative_to(ROOT)), path.read_text()
        spans = re.findall(r"`([^`\n]+)`", text) + [l for l in text.splitlines() if l.startswith("playwright-cli ")]
        for span in spans:
            m = re.search(r"(?:^|&& |; )playwright-cli ((?:(?!&&|\|\||;|\|).)*)$", span)
            toks = (m.group(1) if m else span).split()
            raw = bool(toks) and toks[0] in ("--raw", "--json")
            while toks and (toks[0].startswith("-s=") or toks[0] in ("--raw", "--json")):
                toks.pop(0)
            if not toks or toks[0].startswith("<"):
                continue
            verb = toks[0]
            if verb.startswith("--"):
                if m:
                    yield rel, span, None, toks
            elif m or raw or (rel in PW_DOCS and (verb in PW_VERBS or (re.fullmatch(r"[a-z]+(-[a-z]+)+", verb)
                                                                         and not verb.startswith(PW_NOT_CLI)))):
                yield rel, span, verb, toks[1:]


class PlaywrightCliDocs(unittest.TestCase):
    def test_docs_use_only_real_playwright_cli_verbs_and_flags(self):
        """Every verb and --flag the docs give the agent exists in the installed CLI's --help."""
        seen = set()
        for rel, span, verb, args in pw_commands():
            allowed = PW_GLOBAL
            if verb is not None:
                self.assertIn(verb, PW_VERBS, f"{rel}: `{span}`")
                allowed = PW_GLOBAL | PW_VERBS[verb]
                seen.add(verb)
            for tok in args:
                flag = re.match(r"--([a-z-]+)", tok)
                if flag:
                    self.assertIn(flag.group(1), allowed, f"{rel}: `{span}`")
        # the scan must actually see the commands the explorer depends on
        self.assertLessEqual({"find", "snapshot", "eval", "screenshot", "recording-start", "recording-stop",
                              "console", "requests", "response-body", "route", "unroute", "network-state-set",
                              "generate-locator", "close"}, seen)

    def test_captured_verbs_match_the_installed_cli(self):
        exe = shutil.which("playwright-cli")
        if not exe:
            self.skipTest("playwright-cli not installed")
        out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30).stdout
        live = set(re.findall(r"^  ([a-z][a-z-]+) ", out, re.M))
        used = {verb for _, _, verb, _ in pw_commands() if verb}
        self.assertEqual(used - live, set(), "verbs the docs use that this playwright-cli no longer has")

    def test_bug_box_is_in_screenshot_pixels(self):
        """`screenshot` (no --hires) is CSS pixels, like getBoundingClientRect: no devicePixelRatio factor."""
        tips = (ROOT / "references/playwright-cli-tips.md").read_text()
        explorer = (ROOT / "agents/qa-explorer.md").read_text()
        recipe = tips.split("## Box for a bug image")[1].split("```")[1]
        self.assertNotRegex(recipe, r"\* *d\b|devicePixelRatio;|\* *devicePixelRatio")
        self.assertNotIn("--hires", recipe)
        self.assertNotIn("× `devicePixelRatio`", explorer)
        self.assertIn("no `devicePixelRatio` factor", explorer)


if __name__ == "__main__":
    unittest.main()
