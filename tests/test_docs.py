"""The instruction files and the code must agree: drift here makes agents fail quietly. Contracts only, no wording pins."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from qa_sdet import cli, core, doctor, ops, report
AGENTS = sorted(ROOT.glob("agents/*.md"))
SKILLS = sorted(ROOT.glob("skills/*/SKILL.md"))
DOCS = SKILLS + AGENTS + sorted(ROOT.glob("references/*.md"))
ALL = DOCS + [ROOT / "README.md"]
text = lambda rel: (ROOT / rel).read_text()
# every playwright-cli verb the docs may name (`playwright-cli --help`, 0.1.24)
PW_VERBS = set("""attach check click close close-all config-print console cookie-clear cookie-delete cookie-get cookie-list cookie-set
dblclick delete-data detach dialog-accept dialog-dismiss drag drop eval fill find generate-locator go-back go-forward goto
highlight hover install install-browser keydown keyup kill-all list localstorage-clear localstorage-delete localstorage-get
localstorage-list localstorage-set mousedown mousemove mouseup mousewheel network-state-set open pause-at pdf press
recording-start recording-stop reload request request-body request-headers requests resize response-body response-headers
resume route route-list run-code screenshot select sessionstorage-clear sessionstorage-delete sessionstorage-get
sessionstorage-list sessionstorage-set show snapshot state-load state-save step-over tab-close tab-list tab-new tab-select
tracing-start tracing-stop type uncheck unroute upload video-start video-stop""".split())


def load_guard(tmp):
    """The guard as a hook would run it, with a config that allows qa.example.com and no run marker."""
    cfg = Path(tmp) / "config.json"
    cfg.write_text(json.dumps({"allowedHosts": ["qa.example.com"]}))
    env = {"HOME": tmp, "QA_SDET_DATA": f"{tmp}/data", "QA_SDET_CONFIG": str(cfg)}
    with mock.patch.dict(os.environ, env):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        return SourceFileLoader("guard_docs", str(ROOT / "hooks/guard.py")).load_module(), env


class Docs(unittest.TestCase):
    def test_heuristic_ids_match_code(self):
        self.assertEqual(re.findall(r"^\| ([a-z][a-z0-9-]+) \|", text("references/heuristics.md"), re.M), core.HEURISTICS)

    def test_every_referenced_file_exists(self):
        for f in ALL:
            body = f.read_text()
            for rel in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/\.\./\.\./([\w./-]+)", body):
                self.assertTrue((ROOT / rel).exists(), f"{f.name}: {rel}")
            for name in re.findall(r"`[^`\s]*?([\w-]+\.md)`", body):  # run outputs and the user's qa-rules.md are not plugin files
                if name not in ("REPORT.md", "INBOX.md", "qa-rules.md"):
                    self.assertTrue(any((ROOT / d / name).exists() for d in ("references", "agents", ".")), f"{f.name}: {name}")
            for link in re.findall(r"\]\(([^)#<>\s]+)\)", body):
                self.assertTrue(link.startswith("http") or (f.parent / link).exists(), f"{f.name}: {link}")
        self.assertIn("## Reading REPORT.md", text("README.md"))
        self.assertIn("## Config", text("references/run-setup.md"))

    def test_step_labels_referenced_exist(self):
        for f in SKILLS + AGENTS:
            body = f.read_text()
            defined = set(re.findall(r"^\s*(\d+)\. ", body, re.M))
            for line in body.splitlines():  # a `>` line is a prompt for the explorer: it names the explorer's steps
                if not line.lstrip().startswith(">"):
                    for step in re.findall(r"\bsteps? (\d+)", line):
                        self.assertIn(step, defined, f"{f.parent.name}/{f.name}: step {step}")

    def test_statuses_and_categories_match_code(self):
        explorer, run_json = text("agents/qa-explorer.md"), text("references/run-json.md")
        listed = re.findall(r"^   - ([A-Z_]+):", explorer, re.M)
        self.assertEqual(listed, ["FAIL", "BLOCKED", "QUESTION", "PASS", "NOT_RUN"])  # first that fits: FAIL beats BLOCKED
        self.assertEqual(sorted(listed), sorted(core.CASE_STATUSES))
        self.assertEqual(re.search(r'"category": "([\w |]+)"', run_json).group(1).split(" | "),
                         [k for k, _ in report.NOT_TESTED_CATEGORIES])
        for status in re.findall(r"\b(OPEN|FILED|FIXED|INVALID|REGRESSED)\b", text("skills/qa-memory/SKILL.md")):
            self.assertIn(status, core.STATUSES)

    def test_explorer_reports_the_categories_browser_open_prints(self):
        step1, ops_src = re.search(r"^1\. \*\*Start.*", text("agents/qa-explorer.md"), re.M).group(0), text("lib/qa_sdet/ops.py")
        for category in ("browser profile in use:", "can't start:", "browser tool failed:"):
            self.assertIn(category, step1)
        for printed in ('"browser profile in use:', "can't start\"", '"browser tool failed'):
            self.assertIn(printed, ops_src)

    def test_minutes_cap_is_documented(self):
        with self.assertRaises(core.BadInput):
            cli._count({"--minutes": str(ops.MARKER_HOURS * 60 + 1)}, "--minutes", ops.MARKER_HOURS * 60)
        self.assertIn(f"at most {ops.MARKER_HOURS * 60}", text("references/run-setup.md"))

    def test_readme_verdicts_and_permission_rules_match_code(self):
        readme = text("README.md")
        for icon in report.VERDICT_ICON.values():
            self.assertIn(icon, readme)
        rules = readme.split("## Reading REPORT.md", 1)[1].split("**Verdict per ticket**", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(re.findall(r"^\d\. \S+ ([A-Z ]+?):", rules, re.M), ["FAIL", "BLOCKED", "PASS WITH NOTES", "PASS"])
        t = {"key": "AB-1", "acs": [{"id": "AC-1"}]}
        case = lambda s: {"id": f"TC-{s}", "ticket": "AB-1", "ac": "AC-1", "status": s}
        self.assertEqual(report.verdict(t, [case("FAIL"), case("BLOCKED")], [])[0], "FAIL")
        self.assertEqual(report.verdict(t, [case("BLOCKED"), case("QUESTION")], [])[0], "BLOCKED")
        self.assertEqual(report.verdict(t, [case("QUESTION")], [])[0], "PASS WITH NOTES")
        blocking = report._q_items({"questions": ["Q1 [BLOCKING] x"]}, "test plan")  # rule 3's plan question
        self.assertEqual(report.verdict(t, [case("PASS")], [], questions=blocking)[0], "PASS WITH NOTES")
        block = readme.split("adds these allow rules", 1)[1].split("```", 2)[1]
        self.assertEqual(block.strip().splitlines(), doctor.PERMISSION_RULES)

    def test_planning_example_parses_and_renders(self):
        plan = json.loads(re.search(r"```json\n(.*?)```", text("references/planning.md"), re.S).group(1))
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"QA_SDET_CONFIG": f"{d}/c.json", "QA_SDET_DATA": f"{d}/data"}):
            run = Path(d) / "qa-out/2026-11-24-example"
            (run / ".data").mkdir(parents=True)
            (run / ".data/plan.json").write_text(json.dumps(plan))
            out = report.render_report(run)
        self.assertIn(plan["cases"][0]["id"], out)
        self.assertIn(plan["tickets"][0]["acs"][0]["id"], out)
        self.assertEqual({c["priority"] for c in plan["cases"]} - {"P1", "P2", "P3"}, set())

    def test_code_map_examples_ingest_whole(self):
        """Each example in code-map.md is one mapper's file: the hook ingests it with no row rejected or dropped."""
        from qa_sdet import memory
        blocks = re.findall(r"```json\n(.*?)```", text("references/code-map.md"), re.S)
        self.assertTrue(blocks)
        for n, block in enumerate(blocks):
            with self.subTest(example=n), tempfile.TemporaryDirectory() as d, \
                    mock.patch.dict(os.environ, {"QA_MEM_DB": f"{d}/t.db", "HOME": d}):
                cm, files = json.loads(block.replace("/abs/path/", f"{d}/")), {}
                for r in cm["rows"]:  # the repo files the rows cite, and a scan listing exactly those
                    src = Path(r["repo"], re.sub(r":\d+$", "", r["file"]))
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_text("x")
                    files.setdefault(r["repo"], set()).add(str(src.relative_to(r["repo"])))
                init = Path(d) / "qa-out/init"
                init.mkdir(parents=True)
                (init / f"scan-{cm['area']}.json").write_text(json.dumps({"scan_id": cm["scan_id"], "area": cm["area"], "repos": [
                    {"path": p, "files": sorted(f)} for p, f in files.items()]}))
                (init / f"code-map-{cm['area']}.json").write_text(json.dumps(cm))
                self.assertEqual(memory.ingest_pending(init.parent), 1, memory.PARKED)
                self.assertTrue(Path(d, "t.db").exists())
                self.assertNotIn("dropped rows", (init / f"code-map-{cm['area']}.json.ingested").read_text())

    def test_run_setup_chains_are_approved_by_the_guard(self):
        """Setup and teardown are one Bash call each: the guard must approve every command in the chain itself."""
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            proj = Path(os.path.realpath(d)) / "my project"
            (proj / "qa-out/run").mkdir(parents=True)
            blocks = re.findall(r"```\n(.*?)```", text("references/run-setup.md"), re.S)
            self.assertEqual(len(blocks), 2)  # setup, teardown
            for block in blocks:
                cmd = block.strip().replace("<project>", str(proj)).replace("<run>", f"{proj}/qa-out/run").replace("<route>", "/form")
                cmd = re.sub(r" \[[^\]]*\]", " --budget 5 --faults", cmd)  # the optional flags
                with mock.patch.dict(os.environ, env):
                    os.environ.pop("CLAUDE_PROJECT_DIR", None)
                    for agent in ("", "qa-sdet:qa-explorer"):
                        self.assertEqual(g.evaluate({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(proj),
                                                     "agent_type": agent}), (None, True), cmd)
                    self.assertIn("host", g.check("playwright-cli -s=qa goto /form", str(proj)))  # a bare path has no host
                    self.assertIn("--faults", g.check("playwright-cli -s=qa route '**/api/*'", str(proj)))  # also with no run

    def test_inbox_command_passes_a_custom_jql_whole(self):  # doctor's own example broke the double-quoted --jql
        import ast, shlex
        hint = next(n.value for n in ast.walk(ast.parse((ROOT / "lib/qa_sdet/doctor.py").read_text()))
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and '"inboxJql":' in n.value)
        jql = json.loads("{" + hint.removeprefix("e.g. ") + "}")["inboxJql"]  # status = "Ready for QA" ORDER BY updated DESC
        self.assertIn('"', jql)
        line = next(x for x in text("skills/qa-inbox/SKILL.md").splitlines() if "acli jira workitem search --jql" in x)
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            proj = Path(os.path.realpath(d)) / "my project"
            (proj / "qa-out").mkdir(parents=True)
            for value in (jql, "summary ~ 'it''s' ORDER BY updated DESC"):  # the skill's rule: each ' written '\''
                cmd = line.strip("`").replace("<JQL>", value.replace("'", "'\\''")).replace("${CLAUDE_PROJECT_DIR}", str(proj))
                words = shlex.split(cmd.split(" | ")[0])
                self.assertEqual(words[words.index("--jql") + 1], value, cmd)
                with mock.patch.dict(os.environ, env):
                    self.assertEqual(g.evaluate({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(proj)}),
                                     (None, True), cmd)  # still approved without a prompt

    def test_explorer_signal_dump_is_approved_by_the_guard(self):
        """Step 9 saves the browser signals and closes the browser in one call: the guard must approve all of it."""
        chain = re.search(r"`(playwright-cli -s=qa requests --all [^`]+)`", text("agents/qa-explorer.md")).group(1)
        self.assertIn("console error --all", chain)
        self.assertTrue(chain.endswith("playwright-cli -s=qa close"), chain)
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            proj = Path(os.path.realpath(d)) / "my project"
            (proj / "qa-out/run/.data").mkdir(parents=True)
            cmd = chain.replace("<out>", f"{proj}/qa-out/run").replace("<slug>", "form")
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                self.assertEqual(g.evaluate({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(proj),
                                             "agent_type": "qa-sdet:qa-explorer"}), (None, True), cmd)

    def test_filing_command_quotes_page_text(self):
        """A title quoting page text must not run `$(…)` or backticks in the user's shell."""
        cmd = re.search(r"`(acli jira workitem create [^`]+)`", text("references/run-setup.md")).group(1)
        title = "Status shows `id` and $(echo INJECTED) after Save"
        line = cmd.replace("acli ", "printf '%s\\n' ", 1).replace("<the draft's # title>", title) \
            .replace("<absolute draft path>", "/p/bugs/BUG-1.md").replace("<PROJ>", "DEMO")
        out = subprocess.run(["bash", "-c", line], capture_output=True, text=True).stdout
        self.assertIn(title + "\n", out)
        self.assertNotIn("INJECTED after", out.replace(title, ""))

    def test_caller_labels_reach_the_explorer(self):
        """Labels the skills pass (`RULES:`, qa-explore's --focus as `FOCUS:`) are ones the explorer's Input names."""
        delegate = text("references/run-setup.md").split("## Delegate", 1)[1].split("\n## ", 1)[0]
        labels = set(re.findall(r"`([A-Z]+):", delegate))
        self.assertTrue({"RULES", "FOCUS"} <= labels, labels)
        explorer_input = text("agents/qa-explorer.md").split("## Input", 1)[1].split("\n## ", 1)[0]
        for label in labels:
            self.assertIn(f"`{label}:", explorer_input, label)
        self.assertIn("FOCUS:", text("skills/qa-explore/SKILL.md"))

    def test_duplicate_check_runs_the_known_bugs_search_before_filing(self):
        """Every filing command is preceded by a read-only Jira search the guard lets through."""
        reply = text("references/run-setup.md").split("## Reply", 1)[1]
        self.assertLess(reply.index("acli jira workitem search"), reply.index("acli jira workitem create"))
        self.assertIn("jira.md", reply)
        search = re.search(r"`(acli jira workitem search --jql '[^`]+)`", text("references/jira.md")).group(1)
        cmd = search.replace("<jiraProject>", "DEMO").replace("<2-3 key terms>", "speed limit saved")
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                self.assertIsNone(g.check(cmd, os.path.realpath(d)), cmd)

    def test_no_mcp_fallback_and_no_assumed_browser(self):
        for f in DOCS:
            self.assertNotRegex(f.read_text(), r"(?i)\bmcp\b", f.name)
        for f in ALL:
            self.assertNotRegex(f.read_text(), r"--browser\s*[= ]\s*(chrome|chromium|msedge|firefox|webkit)\b|\"browser\":\s*\"(chrome|msedge|firefox|webkit)\"", f.name)

    def test_playwright_cli_verbs_in_docs_exist(self):
        for f in ALL:
            spans = re.findall(r"`([^`\n]+)`", f.read_text()) + re.findall(r"^playwright-cli .*", f.read_text(), re.M)
            for span in spans:
                for verb in re.findall(r"playwright-cli (?:-s=[\w-]+ )?(?:--raw )?([a-z][a-z-]+)", span):
                    self.assertIn(verb, PW_VERBS, f"{f.name}: `{span}`")

    def test_frontmatter(self):
        fm = lambda p: dict(re.findall(r"^([\w-]+): (.*)$", p.read_text().split("---\n")[1], re.M))
        for path in SKILLS:
            meta, body = fm(path), path.read_text().split("---\n", 2)[2]
            self.assertEqual(meta["name"], path.parent.name)
            if meta.get("disable-model-invocation") != "true":
                self.assertIn("Use when", meta["description"], path.parent.name)
            for flag in re.findall(r"--[a-z]+", meta.get("argument-hint", "")):
                self.assertIn(flag, body, f"{path.parent.name}: {flag}")
        self.assertEqual(fm(ROOT / "skills/qa-setup/SKILL.md").get("disable-model-invocation"), "true")  # it edits Claude Code's settings
        for path in AGENTS:
            meta = fm(path)
            self.assertEqual(meta["name"], path.stem)
            self.assertIn(meta["model"], ("haiku", "sonnet", "opus", "inherit"), path.name)
            self.assertIn(meta["effort"], ("low", "medium", "high", "xhigh", "max"), path.name)

    def test_productivity_workflow_contracts_in_docs(self):
        """Skills and references wire the new session-check, batch explore, resume/retest, bug-sync and prep flows."""
        self.assertIn("qa-mem session-check", text("references/run-setup.md"))
        self.assertIn("qa-mem session-check --record", text("skills/qa-login/SKILL.md"))
        for token in ("qa-mem prep", "qa-mem plan-find", "--resume", "--retest", "--all"):
            self.assertIn(token, text("skills/qa-ticket/SKILL.md"))
        for token in ("--changed", "--top", "qa-mem gaps --changed"):
            self.assertIn(token, text("skills/qa-explore/SKILL.md"))
        for token in ("inboxJql", "qa-mem bug-sync"):
            self.assertIn(token, text("skills/qa-inbox/SKILL.md"))
        self.assertIn("jiraAcField", text("references/jira.md"))
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                self.assertEqual(g.evaluate({"tool_name": "Bash", "tool_input": {"command": "qa-mem session-check --record"},
                                             "cwd": os.path.realpath(d)}), (None, True))


if __name__ == "__main__":
    unittest.main()
