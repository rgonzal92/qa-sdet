"""The instruction files and the code must agree: drift here makes agents fail quietly. Contracts only, no wording pins."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from support import load_hook

from qa_sdet import cli, core, doctor, report

AGENTS = sorted(ROOT.glob("agents/*.md"))
SKILLS = sorted(ROOT.glob("skills/*/SKILL.md"))
DOCS = SKILLS + AGENTS + sorted(ROOT.glob("references/*.md"))
ALL = DOCS + [ROOT / "README.md"]
text = lambda rel: (ROOT / rel).read_text()
# every playwright-cli verb the docs may name (`playwright-cli --help`, 0.1.24, plus the hidden config-print)
PW_VERBS = set(
    """attach check clear-color-scheme clear-contrast clear-forced-colors clear-media clear-reduced-motion click close
close-all config-print console cookie-clear cookie-delete cookie-get cookie-list cookie-set dblclick delete-data detach
dialog-accept dialog-dismiss drag drop eval fill find generate-locator go-back go-forward goto highlight hover install
install-browser keydown keyup kill-all list localstorage-clear localstorage-delete localstorage-get localstorage-list
localstorage-set mousedown mousemove mouseup mousewheel network-state-set open pause-at pdf press recording-start
recording-stop reload request request-body request-headers requests resize response-body response-headers resume route
route-list run-code screenshot select sessionstorage-clear sessionstorage-delete sessionstorage-get sessionstorage-list
sessionstorage-set set-color-scheme set-contrast set-forced-colors set-media set-reduced-motion show snapshot state-load
state-save step-over tab-close tab-list tab-new tab-select tracing-start tracing-stop type uncheck unroute upload
video-chapter video-hide-actions video-show-actions video-start video-stop webmcp-call webmcp-list""".split()
)


def load_guard(tmp):
    """The guard as a hook would run it, with a config that allows qa.example.com and no run marker."""
    cfg = Path(tmp) / "config.json"
    cfg.write_text(json.dumps({"allowedHosts": ["qa.example.com"]}))
    env = {"HOME": tmp, "QA_SDET_DATA": f"{tmp}/data", "QA_SDET_CONFIG": str(cfg)}
    with mock.patch.dict(os.environ, env):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        return load_hook("guard_docs"), env


class Docs(unittest.TestCase):
    def test_heuristic_ids_match_code(self):
        self.assertEqual(
            re.findall(r"^\| ([a-z][a-z0-9-]+) \|", text("references/heuristics.md"), re.M), core.HEURISTICS
        )

    def test_every_referenced_file_exists(self):
        for f in ALL:
            body = f.read_text()
            for rel in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/\.\./\.\./([\w./-]+)", body):
                self.assertTrue((ROOT / rel).exists(), f"{f.name}: {rel}")
            for name in re.findall(
                r"`[^`\s]*?([\w-]+\.md)`", body
            ):  # run outputs and the user's qa-rules.md are not plugin files
                if name not in ("REPORT.md", "INBOX.md", "qa-rules.md", "plugin-digest-YYYY-MM-DD.md"):
                    self.assertTrue(
                        any((ROOT / d / name).exists() for d in ("references", "agents", ".")), f"{f.name}: {name}"
                    )
            for link in re.findall(r"\]\(([^)#<>\s]+)\)", body):
                self.assertTrue(link.startswith("http") or (f.parent / link).exists(), f"{f.name}: {link}")
        self.assertIn("## Reading REPORT.md", text("README.md"))
        self.assertIn("## Config", text("references/run-setup.md"))

    def test_every_script_hooks_json_runs_exists_and_compiles(self):
        hooks = json.loads(text("hooks/hooks.json"))["hooks"]
        cmds = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
        scripts = {rel for c in cmds for rel in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", c)}
        self.assertIn("hooks/pretooluse.py", scripts)
        for rel in sorted(scripts):
            with self.subTest(script=rel):
                path = ROOT / rel
                self.assertTrue(path.is_file(), rel)
                compile(path.read_text(encoding="utf-8"), str(path), "exec")  # a syntax error would break every call

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
        self.assertEqual(
            listed, ["FAIL", "BLOCKED", "QUESTION", "PASS", "NOT_RUN"]
        )  # first that fits: FAIL beats BLOCKED
        self.assertEqual(sorted(listed), sorted(core.CASE_STATUSES))
        self.assertEqual(
            re.search(r'"category": "([\w |]+)"', run_json).group(1).split(" | "),
            [k for k, _ in report.NOT_TESTED_CATEGORIES],
        )
        for status in re.findall(r"\b(OPEN|FILED|FIXED|INVALID|REGRESSED)\b", text("skills/qa-memory/SKILL.md")):
            self.assertIn(status, core.STATUSES)

    def test_explorer_reports_the_categories_browser_open_prints(self):
        step1, ops_src = (
            re.search(r"^1\. \*\*Start.*", text("agents/qa-explorer.md"), re.M).group(0),
            text("lib/qa_sdet/ops.py"),
        )
        for category in ("browser profile in use:", "can't start:", "browser tool failed:"):
            self.assertIn(category, step1)
        for printed in ('"browser profile in use:', "can't start\"", '"browser tool failed'):
            self.assertIn(printed, ops_src)

    def test_readme_verdicts_and_permission_rules_match_code(self):
        readme = text("README.md")
        for icon in report.VERDICT_ICON.values():
            self.assertIn(icon, readme)
        rules = readme.split("## Reading REPORT.md", 1)[1].split("**Verdict per ticket**", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(
            re.findall(r"^\d\. \S+ ([A-Z ]+?):", rules, re.M), ["FAIL", "BLOCKED", "PASS WITH NOTES", "PASS"]
        )
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
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.dict(os.environ, {"QA_SDET_CONFIG": f"{d}/c.json", "QA_SDET_DATA": f"{d}/data"}),
        ):
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
            with (
                self.subTest(example=n),
                tempfile.TemporaryDirectory() as d,
                mock.patch.dict(os.environ, {"QA_MEM_DB": f"{d}/t.db", "HOME": d}),
            ):
                cm, files = json.loads(block.replace("/abs/path/", f"{d}/")), {}
                for r in cm["rows"]:  # the repo files the rows cite, and a scan listing exactly those
                    src = Path(r["repo"], re.sub(r":\d+$", "", r["file"]))
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_text("x")
                    files.setdefault(r["repo"], set()).add(str(src.relative_to(r["repo"])))
                init = Path(d) / "qa-out/init"
                init.mkdir(parents=True)
                (init / f"scan-{cm['area']}.json").write_text(
                    json.dumps(
                        {
                            "scan_id": cm["scan_id"],
                            "area": cm["area"],
                            "repos": [{"path": p, "files": sorted(f)} for p, f in files.items()],
                        }
                    )
                )
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
                cmd = (
                    block.strip()
                    .replace("<project>", str(proj))
                    .replace("<run>", f"{proj}/qa-out/run")
                    .replace("<route>", "/form")
                )
                cmd = re.sub(r" \[[^\]]*\]", " --faults", cmd)  # the optional flags
                with mock.patch.dict(os.environ, env):
                    os.environ.pop("CLAUDE_PROJECT_DIR", None)
                    ev = lambda agent: g.evaluate(
                        {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(proj), "agent_type": agent}  # noqa: B023 - called in this loop pass
                    )
                    self.assertEqual(ev(""), (None, True), cmd)
                    self.assertIn("coordinator", ev("qa-sdet:qa-explorer")[0] or "", cmd)  # they set the run marker
                    self.assertIn(
                        "host", g.check("playwright-cli -s=qa goto /form", str(proj))
                    )  # a bare path has no host
                    self.assertIn(
                        "--faults", g.check("playwright-cli -s=qa route '**/api/*'", str(proj))
                    )  # also with no run

    def test_inbox_command_passes_a_custom_jql_whole(self):  # doctor's own example broke the double-quoted --jql
        import ast
        import shlex

        hint = next(
            n.value
            for n in ast.walk(ast.parse((ROOT / "lib/qa_sdet/doctor.py").read_text()))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and '"inboxJql":' in n.value
        )
        jql = json.loads("{" + hint.removeprefix("e.g. ") + "}")[
            "inboxJql"
        ]  # status = "Ready for QA" ORDER BY updated DESC
        self.assertIn('"', jql)
        line = next(x for x in text("skills/qa-inbox/SKILL.md").splitlines() if "acli jira workitem search --jql" in x)
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            proj = Path(os.path.realpath(d)) / "my project"
            (proj / "qa-out").mkdir(parents=True)
            for value in (jql, "summary ~ 'it''s' ORDER BY updated DESC"):  # the skill's rule: each ' written '\''
                cmd = (
                    line.strip("`")
                    .replace("<JQL>", value.replace("'", "'\\''"))
                    .replace("${CLAUDE_PROJECT_DIR}", str(proj))
                )
                words = shlex.split(cmd.split(" | ")[0])
                self.assertEqual(words[words.index("--jql") + 1], value, cmd)
                with mock.patch.dict(os.environ, env):
                    self.assertEqual(
                        g.evaluate({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(proj)}),
                        (None, True),
                        cmd,
                    )  # still approved without a prompt

    def test_explorer_signal_dump_is_approved_by_the_guard(self):
        """Step 9 saves the browser signals and closes the browser in one call: the guard must approve all of it."""
        chains = re.findall(r"`(playwright-cli -s=qa requests --all [^`]+)`", text("agents/qa-explorer.md"))
        self.assertTrue(all("console error --all" in c for c in chains), chains)
        self.assertTrue(any(c.endswith("playwright-cli -s=qa close") for c in chains), chains)  # save, then close
        self.assertTrue(any("qa-mem signals-triage" in c for c in chains), chains)  # triage first, browser still open
        self.assertTrue(
            any("qa-mem body-check '<out>' --slug <slug> --all; qa-mem signals-triage" in c for c in chains)
        )
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            proj = Path(os.path.realpath(d)) / "my project"
            (proj / "qa-out/run/.data").mkdir(parents=True)
            for chain in chains:
                cmd = chain.replace("<out>", f"{proj}/qa-out/run").replace("<slug>", "form")
                with mock.patch.dict(os.environ, env):
                    os.environ.pop("CLAUDE_PROJECT_DIR", None)
                    self.assertEqual(
                        g.evaluate(
                            {
                                "tool_name": "Bash",
                                "tool_input": {"command": cmd},
                                "cwd": str(proj),
                                "agent_type": "qa-sdet:qa-explorer",
                            }
                        ),
                        (None, True),
                        cmd,
                    )

    def test_the_explorer_is_told_which_calls_run_at_once_and_the_guard_agrees(self):
        """One word the guard doesn't approve sends a whole call to Claude Code's slow permission check: the explorer's
        Bash paragraph names the words that run at once, and a chain of them is approved for it."""
        para = next(x for x in text("agents/qa-explorer.md").splitlines() if x.startswith("Write each Bash call"))
        words = "grep head tail cut sort uniq wc tr awk echo date ls".split()
        for word in (*words, "sed -n 'N,Mp'", "`<out>/scratch/`", "slow permission check"):
            self.assertIn(word, para)
        self.assertNotIn("Read the `.playwright-cli/page-", para)  # a whole snapshot in the context: grep it instead
        self.assertIn("`<out>/scratch/", text("agents/qa-explorer.md").split("## Files you write")[1].split("\n\n")[0])
        steps_row = text("references/playwright-cli-tips.md").split("| Several steps |")[1].split("\n")[0]
        self.assertIn("scratch/", steps_row)
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            proj = Path(os.path.realpath(d)) / "proj"
            (proj / "qa-out/run/scratch").mkdir(parents=True)
            chain = (
                "date -u +%Y%m%d-%H%M; echo ---; playwright-cli -s=qa click e3 && sleep 1; "
                f"playwright-cli -s=qa snapshot > '{proj}/qa-out/run/scratch/page.yml'; "
                f"grep -n Save '{proj}/qa-out/run/scratch/page.yml' | head -5 | tail -2; ls '{proj}/qa-out/run/scratch'; "
                "playwright-cli -s=qa snapshot | sed -n '1,40p' | cut -c1-200 | tr -s ' ' | awk '{print $1}' "
                "| sort | uniq -c | wc -l"
            )
            call = {"tool_name": "Bash", "tool_input": {"command": chain}, "cwd": str(proj)}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(g.evaluate(call | {"agent_type": "qa-sdet:qa-explorer"}), (None, True))
                find = "playwright-cli -s=qa find Save"
                for slow in ("X=1; " + chain, chain + " | jq .", chain + " || true", "cd /tmp && " + find):
                    self.assertEqual(
                        g.evaluate(call | {"agent_type": "qa-explorer", "tool_input": {"command": slow}}), (None, False)
                    )

    def test_the_body_check_pipeline_is_approved_and_named_by_the_explorer_and_the_tips(self):
        """Step 9 reads a body into `qa-mem body-check`: the verb exists, the guard lets the pipe through, both docs name it."""
        self.assertIn("body-check", cli.COMMANDS)
        for doc in ("agents/qa-explorer.md", "references/playwright-cli-tips.md"):
            self.assertIn("| qa-mem body-check '<out>' --slug <slug> <#>", text(doc), doc)
        cmd = "playwright-cli -s=qa response-body 7 | qa-mem body-check '/p/qa-out/run' --slug form 7"
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            proj = Path(os.path.realpath(d)) / "proj"
            (proj / "qa-out/run/.data").mkdir(parents=True)
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                self.assertIsNone(g.check(cmd.replace("/p/", f"{proj}/"), str(proj), explorer=True), cmd)

    def test_the_broken_output_probe_sees_fixed_elements_and_the_explorer_runs_it(self):
        """D#3: the probe was never called, and `offsetParent` is null for every position: fixed toast and modal."""
        tips = text("references/playwright-cli-tips.md")
        probe = tips.split("## Look for broken output")[1].split("```")[1].strip()
        self.assertNotIn("offsetParent", probe)
        for want in (
            "getBoundingClientRect",
            "getComputedStyle",
            "elementFromPoint",
            "duplicate id",
            r"\{\{",
            "NaN",
            "zero count",
        ):
            self.assertIn(want, probe)
        self.assertEqual(
            probe.count('"'), 2
        )  # one double-quoted shell word: no inner `"`, backtick or `$` the shell would expand
        self.assertNotIn("`", probe)
        self.assertNotRegex(probe, r"\$[{(\w]")
        step5 = text("agents/qa-explorer.md").split("5. **Cases, then the check loop.**")[1].split("\n6. ")[0]
        self.assertIn("Look for broken output", step5)
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                self.assertIsNone(g.check(probe, os.path.realpath(d), explorer=True), probe[:80])

    def test_the_browser_condition_recipes_pass_the_guard_and_the_fixtures_are_the_ones_named(self):
        """Narrow window, dark mode, dirty-form dialog and uploads: every command the tips give is allowed to the explorer,
        and an upload from the plugin's references folder needs no guard change."""
        tips = text("references/playwright-cli-tips.md")
        probe = tips.split("## Look for broken output")[1].split("```")[1].strip()
        script = probe.split('--raw eval "', 1)[1].rsplit('"', 1)[0]
        section = tips.split("## Browser conditions")[1].split("\n## ")[0]
        fixtures = ROOT / "references/upload-fixtures"
        names = re.findall(r"upload '<refs>/upload-fixtures/([\w.-]+)'", section)
        self.assertEqual(sorted(names), sorted(p.name for p in fixtures.iterdir()))  # the three, no others
        self.assertTrue(all(p.stat().st_size < 1024 and p.name.startswith("qa-sdet-") for p in fixtures.iterdir()))
        self.assertEqual((fixtures / "qa-sdet-empty.txt").stat().st_size, 0)
        self.assertEqual((fixtures / "qa-sdet-1x1.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertNotIn(b"%PDF", (fixtures / "qa-sdet-fake.pdf").read_bytes())  # text with a .pdf name, not a PDF
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            cmds = []
            for block in section.split("```")[1::2]:  # a command starts at `playwright-cli`; a script may span lines
                for line in block.strip().splitlines():
                    cmds.append(line) if line.startswith("playwright-cli") else cmds.__setitem__(
                        -1, cmds[-1] + "\n" + line
                    )
            self.assertEqual(len(cmds), 7)  # window size, narrow chain, dark eval, dark chain, three uploads
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                for cmd in cmds:
                    cmd = (
                        cmd.replace("<the broken-output probe's script>", script)
                        .replace("<the same script>", "1")
                        .replace("<refs>", str(ROOT / "references"))
                        .replace("<the width> <the height>", "1280 800")
                    )
                    self.assertIsNone(g.check(cmd, os.path.realpath(d), explorer=True), cmd[:120])
        self.assertIn("Never `dialog-accept`", section)
        self.assertIn("FOCUS:", section.split("**Dark mode**")[1].split("**Unsaved edits**")[0])
        self.assertIn("Browser conditions", text("agents/qa-explorer.md"))

    def test_the_tab_typing_and_settling_recipes_pass_the_guard_and_the_explorer_names_them(self):
        """A popup tab, a field typed key by key, a count read until it settles, a control that moved: the explorer is
        sent to each section by its title, and every command there is allowed to it."""
        tips, explorer = text("references/playwright-cli-tips.md"), text("agents/qa-explorer.md")
        fill = {
            "<locator of the first part>": "getByLabel('Hours')",
            "<css of the parts>": "#time input",
            "<css of the count>": "#range",
            "<locator>": "getByRole('button', { name: 'Save' })",
            "<css of what appears>": "[role=status]",
        }
        settled = tips.split("## Settled, not just loaded")[1].split("\n## ")[0]
        self.assertIn("no `sleep`", settled)  # explorers with no wait recipe slept after clicks and polled with `find`
        self.assertIn("not there after 10 s", settled)
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                for title, commands in (
                    ("Second tab or popup", 2),
                    ("Type key by key", 1),
                    ("Settled, not just loaded", 2),
                    ("A control that moved", 0),
                ):
                    self.assertIn(f'"{title}"', explorer)
                    section = tips.split(f"## {title}")[1].split("\n## ")[0]
                    cmds = [x for block in section.split("```")[1::2] for x in block.strip().splitlines()]
                    self.assertEqual(len(cmds), commands, title)
                    for cmd in cmds:
                        for mark, value in fill.items():
                            cmd = cmd.replace(mark, value)
                        self.assertNotIn("<", cmd.split("--raw eval")[0], cmd)  # every placeholder has a stand-in here
                        self.assertIsNone(g.check(cmd, os.path.realpath(d), explorer=True), cmd[:120])
        self.assertIn(
            "tab-select 0", tips.split("## Second tab or popup")[1].split("\n## ")[0]
        )  # back to the first tab

    def test_filing_command_quotes_page_text(self):
        """A title quoting page text must not run `$(…)` or backticks in the user's shell."""
        cmd = re.search(r"`(acli jira workitem create [^`]+)`", text("references/run-setup.md")).group(1)
        title = "Status shows `id` and $(echo INJECTED) after Save"
        line = (
            cmd.replace("acli ", "printf '%s\\n' ", 1)
            .replace("<the draft's # title>", title)
            .replace("<absolute draft path>", "/p/bugs/BUG-1.md")
            .replace("<PROJ>", "DEMO")
        )
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
            self.assertNotRegex(
                f.read_text(),
                r"--browser\s*[= ]\s*(chrome|chromium|msedge|firefox|webkit)\b|\"browser\":\s*\"(chrome|msedge|firefox|webkit)\"",
                f.name,
            )

    def test_playwright_cli_verbs_in_docs_exist(self):
        for f in ALL:
            spans = re.findall(r"`([^`\n]+)`", f.read_text()) + re.findall(r"^playwright-cli .*", f.read_text(), re.M)
            for span in spans:
                for verb in re.findall(r"playwright-cli (?:-s=[\w-]+ )?(?:--raw )?([a-z][a-z-]+)", span):
                    self.assertIn(verb, PW_VERBS, f"{f.name}: `{span}`")

    def test_the_batch_eval_recipe_and_the_link_harvester_keep_their_fixes(self):
        tips = text("references/playwright-cli-tips.md")
        recipe = tips.split("Many values in one eval")[1].split("```")[1]
        for want in ("HTMLTextAreaElement", "HTMLSelectElement", "el.tagName", "checkValidity()", "requestSubmit()"):
            self.assertIn(want, recipe)  # the native setter of each tag; a form the browser blocks is never submitted
        self.assertNotIn("setTimeout(r, 300)", recipe)  # a bounded wait for the answer, not a fixed 300 ms
        self.assertLess(recipe.index("checkValidity()"), recipe.index("requestSubmit()"))
        harvester = tips.split("## Map the page's states and links")[1].split("```")[1]
        self.assertNotIn("u.search", harvester)  # one path is one page: ?page=2 and ?sort=x are not new pages
        from qa_sdet import crawl  # qa-mem discover runs this harvester: one copy for the explorer and the crawl

        said = harvester.split('--raw eval "', 1)[1].rsplit('"', 1)[0]
        self.assertEqual(" ".join(crawl.HARVESTER_JS.split()), " ".join(said.split()))
        self.assertIn("u.hash.startsWith('#/')", said)  # an app routed by the hash is many pages at one path
        states_loop = said.split("out.states.push")[0].rsplit("for (", 1)[1]
        self.assertIn(core.SKIP_LINK_RE.pattern, states_loop)  # a "Delete" menu item is no state to open
        self.assertIn(core.SKIP_LINK_RE.pattern, said.split("for (const el")[0])  # the links keep the same filter
        self.assertNotRegex(said, r'[$`"\\]')  # it sits in a double-quoted shell string

    def test_no_doc_passes_cursor_to_video_show_actions(
        self,
    ):  # 0.1.24 reads `--cursor none` as a boolean plus an argument
        for f in ALL:
            self.assertNotRegex(f.read_text(), r"video-show-actions[^\n`]*--cursor", f.name)

    def test_api_writes_flag_reaches_the_marker_the_brief_and_every_doc(self):
        self.assertIn("--api-writes", cli.COMMANDS["dispatch"])
        self.assertIn("--api-writes", cli.COMMANDS["marker"])
        for rel in ("skills/qa-explore/SKILL.md", "skills/qa-ticket/SKILL.md", "references/run-setup.md", "README.md"):
            self.assertIn("--api-writes", text(rel), rel)
        for rel in ("skills/qa-explore/SKILL.md", "skills/qa-ticket/SKILL.md"):
            self.assertIn("--api-writes", text(rel).split("---\n")[1], rel)  # the argument-hint
        self.assertIn("api_writes", text("agents/qa-explorer.md"))  # the brief line it reads

    def test_server_boundary_recipes_pass_the_guard_and_replace_curl(self):
        """Every command of the tips file's "Server-boundary probes" is approved for the explorer in a run with api_writes
        (and its writes are refused without it); the explorer no longer sends curl for a server rule."""
        explorer = text("agents/qa-explorer.md")
        self.assertNotIn("curl to an allowed host", explorer)
        for token in ("Server-boundary probes", "qa-mem api-probe", "api_writes: yes", "Left in the app"):
            self.assertIn(token, explorer)
        self.assertIn("Server-boundary probes", text("references/heuristics.md"))
        section = text("references/playwright-cli-tips.md").split("## Server-boundary probes")[1].split("\n## ")[0]
        blocks = re.findall(r"```\n(.*?)```", section, re.S)
        self.assertEqual(len(blocks), 4)  # the UI bypass, the reads, the writes, the encoding canary
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            proj = Path(os.path.realpath(d)) / "proj"
            (proj / "qa-out").mkdir(parents=True)
            marker = lambda **m: (proj / "qa-out/.active").write_text(json.dumps({"expires": time.time() + 3600, **m}))
            with mock.patch.dict(os.environ, env):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                ev = lambda cmd: g.evaluate(
                    {
                        "tool_name": "Bash",
                        "tool_input": {"command": cmd},
                        "cwd": str(proj),
                        "agent_type": "qa-sdet:qa-explorer",
                    }
                )
                cmds = [
                    re.sub("<qaBaseUrl>", "https://qa.example.com", c.replace("<#>", "3").replace("<id>", "7"))
                    for b in blocks
                    for c in re.split(r"\n(?=playwright-cli |qa-mem )", b.strip())
                ]
                writes = [c for c in cmds if re.match(r"qa-mem api-probe (POST|PUT|PATCH|DELETE) ", c)]
                self.assertEqual(len(writes), 2)
                for api_writes in (True, False):
                    marker(api_writes=api_writes)
                    for cmd in cmds:
                        got = ev(cmd)
                        if cmd in writes and not api_writes:
                            self.assertIn("--api-writes", got[0] or "", cmd)
                        else:
                            self.assertEqual(got, (None, True), cmd)

    def test_frontmatter(self):
        fm = lambda p: dict(re.findall(r"^([\w-]+): (.*)$", p.read_text().split("---\n")[1], re.M))
        for path in SKILLS:
            meta, body = fm(path), path.read_text().split("---\n", 2)[2]
            self.assertEqual(meta["name"], path.parent.name)
            if meta.get("disable-model-invocation") != "true":
                self.assertIn("Use when", meta["description"], path.parent.name)
            for flag in re.findall(r"--[a-z]+", meta.get("argument-hint", "")):
                self.assertIn(flag, body, f"{path.parent.name}: {flag}")
        self.assertEqual(
            fm(ROOT / "skills/qa-setup/SKILL.md").get("disable-model-invocation"), "true"
        )  # it edits Claude Code's settings
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
                self.assertEqual(
                    g.evaluate(
                        {
                            "tool_name": "Bash",
                            "tool_input": {"command": "qa-mem session-check --record"},
                            "cwd": os.path.realpath(d),
                        }
                    ),
                    (None, True),
                )

    def test_retest_cases_come_from_the_prior_plan_with_their_steps(self):
        """plan-find's retest_cases name the cases (no steps or expected result); prior_passed entries are complete. The
        skill must copy the prior plan's case objects for those ids, or the explorer gets cases it can't run."""
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"QA_SDET_CONFIG": f"{d}/c.json"}):
            run = Path(d) / "qa-out/2026-11-24-PROJ-1"
            (run / ".data").mkdir(parents=True)
            case = {
                "id": "TC-1",
                "ticket": "PROJ-1",
                "ac": "AC-1",
                "title": "t",
                "route": "/f",
                "steps": ["s"],
                "expected": "e",
            }
            (run / ".data/plan.json").write_text(
                json.dumps({"kind": "ticket", "tickets": [{"key": "PROJ-1"}], "cases": [case]})
            )
            (run / ".data/run-f.json").write_text(
                json.dumps({"target": "/f", "cases": [{"id": "TC-1", "status": "FAIL"}]})
            )
            entry = report._run_state(run)["retest_cases"][0]
        self.assertFalse(
            {"steps", "expected"} & set(entry)
        )  # copying these verbatim would drop what the explorer checks
        retest = next(x for x in text("skills/qa-ticket/SKILL.md").splitlines() if x.startswith("- **`--retest"))
        self.assertIn(
            "the prior plan's case objects (steps and expected included) whose ids are in `retest_cases`", retest
        )

    def test_batch_explore_skips_routes_that_need_a_record_id(self):
        """`gaps --json` lists detail routes under `needs_id`; the batch must leave them out, not open '/x/:id'."""
        skill = text("skills/qa-explore/SKILL.md")
        self.assertIn("`needs_id`", skill)
        self.assertIn('"needs_id"', text("lib/qa_sdet/memory.py"))  # the key gaps --json writes

    def test_explorer_tools_are_the_ones_the_guard_allows(self):  # a denylist inherited every MCP server of the session
        meta = dict(re.findall(r"^([\w-]+): (.*)$", text("agents/qa-explorer.md").split("---\n")[1], re.M))
        self.assertNotIn("disallowedTools", meta)
        with tempfile.TemporaryDirectory() as d:
            g, _ = load_guard(os.path.realpath(d))
        tools = {t.strip() for t in meta["tools"].split(",")}
        self.assertLessEqual(tools, g.EXPLORER_TOOLS)
        self.assertNotIn("Agent", tools)
        for name in re.findall(r"`<out>/(bugs|repro|evidence|\.data)/([^`]+)`", text("agents/qa-explorer.md")):
            self.assertIn(name[0], g.EXPLORER_FILES)

    def test_leftover_test_data_line_is_the_one_the_report_reads(self):
        explorer = text("agents/qa-explorer.md")
        self.assertIn("`Left in the app: ", explorer)
        self.assertIn("Left in the app:", text("lib/qa_sdet/report.py"))

    def test_bug_sync_search_reads_every_page_and_is_approved(self):  # without --paginate acli stops at its first page
        line = next(x for x in text("skills/qa-inbox/SKILL.md").splitlines() if "| qa-mem bug-sync" in x)
        cmd = (
            re.search(r"`(acli jira workitem search [^`]+\| qa-mem bug-sync)`", line)
            .group(1)
            .replace("<that JQL>", "key in (P-1)")
        )
        self.assertIn("--paginate", cmd)
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            (Path(os.path.realpath(d)) / "qa-out").mkdir()
            with mock.patch.dict(os.environ, env):
                self.assertEqual(
                    g.evaluate({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": os.path.realpath(d)}),
                    (None, True),
                    cmd,
                )

    def test_crawl_and_discover_are_qa_mem_commands_not_an_agent(self):
        # found live: one 20-turn agent for 19 routes and ~1,280 locators ran out of turns twice and saved almost nothing
        self.assertFalse((ROOT / "agents/qa-crawler.md").exists())
        init, explore = text("skills/qa-init/SKILL.md"), text("skills/qa-explore/SKILL.md")
        self.assertIn("qa-mem crawl '<run>'", init)
        self.assertIn("qa-mem discover '<run>'", explore)
        for skill in (init, explore):
            self.assertIn("run_in_background", skill)  # minutes long: a foreground Bash call ends at 10 minutes
        self.assertIn("crawl", cli.COMMANDS)
        self.assertIn("discover", cli.COMMANDS)
        hooks = json.loads(text("hooks/hooks.json"))["hooks"]
        self.assertNotIn("qa-crawler", hooks["SubagentStop"][0]["matcher"])
        self.assertNotIn("Mode `map`", text("agents/qa-explorer.md"))
        for f in ALL + [ROOT / "hooks/guard.py", ROOT / "hooks/posttooluse.py"]:
            self.assertNotIn("qa-crawler", f.read_text(), f.name)

    def test_the_explorer_stops_on_every_page_start_blocker(self):  # found live: it reached a 404 target via goto
        step1 = re.search(r"^1\. \*\*Start.*", text("agents/qa-explorer.md"), re.M).group(0)
        for scope in ("`browser`", "`session`", "`page`"):
            self.assertIn(scope, step1)
        self.assertIn("don't reach it another way", step1)

    def test_continue_and_adaptive_use_explore_next(self):
        skill = text("skills/qa-explore/SKILL.md")
        for flag in ("--continue", "--adaptive"):
            self.assertIn(flag, skill.split("---")[1])  # in argument-hint
            self.assertIn(f"**`{flag}", skill)
        self.assertIn("qa-mem explore-next", skill)
        self.assertIn("up to 2 more pages", skill)  # the settled cap on adaptive expansion
        self.assertIn('"leads"', text("references/run-json.md"))

    def test_the_graph_surfaces_are_documented(self):
        for doc in ("README.md", "skills/qa-memory/SKILL.md"):
            self.assertIn("impact <route|", text(doc))
        self.assertIn("impact", cli.COMMANDS)
        for doc in ("skills/qa-plan/SKILL.md", "skills/qa-ticket/SKILL.md"):
            self.assertIn("impacted_routes", text(doc))
        self.assertIn("impacted_routes", text("lib/qa_sdet/memory.py"))  # the key prep's context carries

    def test_hypothesis_and_target_route_reach_the_cli_the_skill_the_setup_and_the_explorer(self):
        for flag in ("--hypothesis", "--target-route"):
            for doc in ("README.md", "skills/qa-explore/SKILL.md", "references/run-setup.md"):
                self.assertIn(flag, text(doc), f"{doc}: {flag}")
            self.assertIn(flag, cli.COMMANDS["dispatch"])
        skill = text("skills/qa-explore/SKILL.md")
        for word in ("`frontier`", "`run_patterns`", "`entry_route`", "`entry_chain`"):
            self.assertIn(word, skill)
        self.assertIn("HYPOTHESIS:", text("agents/qa-explorer.md"))
        self.assertIn("test data", text("agents/qa-explorer.md"))
        for key in ("frontier", "run_patterns"):  # the keys explore-next writes
            self.assertIn(f'"{key}"', text("lib/qa_sdet/report.py"))

    def test_no_doc_or_agent_offers_a_run_limit(self):  # every run goes until the agent reaches its verdict
        gone = re.compile(
            r"--budget|--minutes|--max-turns|--total-budget|--total-minutes|--more-budget|qa-mem spend"
            r"|turn_limit|budget_limit|time_limit|maxTurns"
        )
        for doc in [ROOT / "README.md", *DOCS]:
            self.assertIsNone(gone.search(doc.read_text()), doc.name)
        for name in ("marker", "dispatch"):
            self.assertIsNone(gone.search(cli.COMMANDS[name]), name)
        self.assertNotIn("spend", cli.COMMANDS)
        # a run Claude Code or the user cuts short still keeps its results
        self.assertIn("INCOMPLETE", text("agents/qa-explorer.md").split("**Cases, then the check loop.**")[1])

    def test_stop_reason_and_the_expansion_gate_are_documented(self):
        from qa_sdet import memory

        for reason in memory.STOP_REASONS:
            self.assertIn(reason, text("references/run-json.md"), reason)
        self.assertIn("stop_reason", text("agents/qa-explorer.md"))
        skill = text("skills/qa-explore/SKILL.md")
        for word in ("expand_recommended", "stop_reason"):
            self.assertIn(word, skill)
        self.assertIn("expand_recommended", text("README.md"))
        self.assertIn('"expand_recommended"', text("lib/qa_sdet/report.py"))

    def test_observed_endpoints_are_documented_where_gaps_and_the_card_are(self):
        self.assertIn("unexercised_apis", text("README.md"))
        self.assertIn("unexercised_apis", text("skills/qa-memory/SKILL.md"))
        self.assertIn("UNEXERCISED ENDPOINTS", text("agents/qa-explorer.md"))
        self.assertIn("UNEXERCISED ENDPOINTS", text("lib/qa_sdet/memory.py"))  # the card line the explorer prompt names

    def test_a_ticket_run_stops_after_its_cases(self):  # 20 of 63 post-case minutes went to the card's open lists
        explorer = text("agents/qa-explorer.md")
        step4 = explorer.split("4. **Read the card**")[1].split("\n5. ")[0]
        step5 = explorer.split("5. **Cases, then the check loop.**")[1].split("\n6. ")[0]
        for label in ("UNVISITED STATES (", "UNEXERCISED ENDPOINTS ("):  # what dispatch drops from a ticket brief
            self.assertIn(label, text("lib/qa_sdet/ops.py"))
        self.assertIn("explore-mode lists (a ticket brief doesn't carry them)", step4)
        self.assertIn(
            "Never open a state that deletes, removes or signs out.", step4
        )  # nor do card or harvester list it
        self.assertIn("in ticket mode try only the boundary and negative values of the fields the AC name", step5)
        self.assertIn("in ticket mode, only for fields a given case or AC names", step5)  # server boundary
        self.assertIn("in explore mode, or when an AC names the condition", step5)  # browser conditions
        done = next(x for x in step5.splitlines() if x.startswith("   - ticket mode:"))
        self.assertIn("boundary and negative values of the fields the AC name", done)
        self.assertNotIn("risks next to them", done)  # open-ended: the run never knew it was done

    def test_blindspots_is_a_command_and_documented(self):
        self.assertIn("blindspots", cli.COMMANDS)
        for doc in ("README.md", "skills/qa-memory/SKILL.md"):
            self.assertIn("blindspots", text(doc), doc)
        for section in (
            "UNVISITED STATES",
            "UNEXERCISED ENDPOINTS",
            "UNTESTED TARGETS",
        ):  # what the stop check reads in the brief
            self.assertIn(section, text("lib/qa_sdet/report.py"))
            self.assertIn(section, text("README.md") + text("lib/qa_sdet/memory.py"))

    def test_the_spec_template_passes_the_checker(self):  # the contract and qa-mem audit-bugs must not disagree
        from qa_sdet import report

        tmpl = re.search(r"```ts\n(.*?)```", text("references/bug-draft.md"), re.S).group(1)
        body = (
            tmpl
            + "test('t', async ({ page }) => { await page.goto('/form'); await expect(page.getByRole('status')).toBeVisible(); });\n"
        )
        self.assertEqual(report.spec_issues(body.replace("<qaBaseUrl>", "https://qa.example")), [])

    def test_the_critic_is_opt_in_and_read_only(self):
        meta = dict(re.findall(r"^([\w-]+): (.*)$", text("agents/qa-reviewer.md").split("---\n")[1], re.M))
        self.assertEqual((meta["tools"], meta["model"]), ("Read", "sonnet"))
        for skill in ("qa-ticket", "qa-explore"):
            body = text(f"skills/{skill}/SKILL.md")
            self.assertIn("--verify", body.split("---\n")[1])  # in argument-hint
            self.assertIn("qa-sdet:qa-reviewer", body)
            self.assertIn(".data/review.json", body)

    def test_plugin_log_and_feedback_skill_match_code(self):
        self.assertIn("plugin-log", cli.COMMANDS)
        self.assertTrue((ROOT / "skills/qa-feedback/SKILL.md").is_file())
        for rel in ("README.md", "skills/qa-setup/SKILL.md", "hooks/guard.py", "hooks/denied.py"):
            self.assertNotIn("guard-denials.log", text(rel), rel)
            self.assertIn("plugin-log.jsonl", text(rel), rel)
        self.assertIn("pluginLog", text("README.md"))
        with tempfile.TemporaryDirectory() as d:
            g, env = load_guard(os.path.realpath(d))
            self.assertIn("plugin-log", g.MANUAL_WRITES)
            proj = Path(os.path.realpath(d)) / "proj"
            proj.mkdir()
            with mock.patch.dict(os.environ, env):
                cmd = f"qa-mem plugin-log note --project '{proj}' 'test note'"
                self.assertEqual(
                    g.evaluate({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(proj)}), (None, True)
                )
                self.assertIsNotNone(
                    g.evaluate(
                        {
                            "tool_name": "Bash",
                            "tool_input": {"command": cmd},
                            "cwd": str(proj),
                            "agent_type": "qa-sdet:qa-explorer",
                        }
                    )[0]
                )


if __name__ == "__main__":
    unittest.main()
