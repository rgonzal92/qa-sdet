"""hooks/guard.py, the PreToolUse guard (or the file named in env GUARD): its 8 rules, the odd forms it won't read
(denied when the call names a guarded tool or path), the run marker, the qa-mapper, approval, the denial log and the
hook plumbing. Everything runs in temp folders: HOME, the config and the data folder."""

import ast
import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from guard_support import (
    ACLI_W,
    CFG,
    CK,
    EX,
    GUARD,
    MAIN,
    MAPPER,
    PROJ,
    ROOT,
    RUN,
    TMP,
    Case,
    D,
    Q,
    bash,
    g,
    gl,
    gp,
    hook,
    project,
    restore_env,
    tool,
    use_env,
)

setUpModule, tearDownModule = use_env, restore_env  # this module's tests run in the guard's sandbox


DEV_ALLOW = [
    "git status && npm test",
    "cat README.md | grep acli",
    "echo 'acli create' > notes.txt",
    "make -j4 2>&1 | tee build.log",
    "npm run build",
    "docker ps -a",
    "sed -i 's/a/b/' f.txt",
    "rm -rf node_modules dist",
    "find . -name '*.pyc' -delete",
    "rm -f ~/*.log",
    "tar xzf a.tgz -C ~",
    "mv notes.txt ~/",
    "grep -rn 'qa-sdet/profiles' skills agents",
    "git commit -m 'guard: deny acli create'",
    'for f in *.md; do wc -l "$f"; done',
    "sqlite3 -readonly ~/.local/share/qa-sdet/app.db .tables",
    "command -v playwright-cli && playwright-cli --version",
    'claude --plugin-dir . -p "/qa-sdet:qa-explore http://localhost:8765/form.html"',
    "ls -la | grep foo",
    "git log --oneline -5 # recent",
    "echo 'a#b' user@host",
    "grep -rn 'https://x.com' src",
    "ssh host 'uptime'",
    "cat > notes.md <<EOF\nDon't forget qa-sdet and acli create\nEOF",
    "echo To file run: acli jira workitem create --summary x",
    "# don't run playwright-cli here\nls",
    "echo $((2+3))",
    "n=$((x>>1)); m=$((a<b))",
    "git log \\\n  --oneline -3",
    "git commit -m \"$(cat <<'EOF'\ndocs: sign out by deleting ~/.local/share/qa-sdet/profiles/\nEOF\n)\"",
    "gh pr create --title x --body=\"$(cat <<'EOF'\nthe guard keeps ~/.local/share/qa-sdet closed\nEOF\n)\"",
]


class DevWork(Case):
    def test_normal_dev_work_passes(self):
        self.table(DEV_ALLOW, False, agents=(False,))


class Plumbing(Case):
    def test_pretooluse_gives_guard_output(self):
        for d in [
            {"tool_name": "Bash", "tool_input": {"command": ACLI_W}, "cwd": str(PROJ)},
            {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": str(PROJ)},
            "not json",
        ]:
            want = hook(d, "guard.py")
            self.assertEqual(hook(d, "pretooluse.py"), want, d)
            self.assertEqual(hook(d, "pretooluse.py", PYTHONSAFEPATH="1"), want, d)
        self.assertIn(
            '"deny"',
            hook({"tool_name": "Bash", "tool_input": {"command": ACLI_W}}, "pretooluse.py", PYTHONSAFEPATH="1"),
        )

    def test_crash_fallback(self):
        def run(data):
            out = io.StringIO()
            with (
                mock.patch.object(g, "evaluate", side_effect=RuntimeError("x"), create=True),
                mock.patch.object(g, "verdict", side_effect=RuntimeError("x"), create=True),
                mock.patch.object(g, "decide", side_effect=RuntimeError("x")),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))),
                contextlib.redirect_stdout(out),
            ):
                g.main()
            return "deny" in out.getvalue()

        bashd = lambda cmd, **kw: {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(PROJ), **kw}
        self.assertTrue(run(bashd("playwright-cli goto x")))
        self.assertTrue(run(bashd(f"cat {D}/x")))
        self.assertTrue(run(bashd("cat ~/.local/share/qa-sdet/profiles/main/x")))
        self.assertTrue(run(bashd(f"cat {CFG}", agent_type=EX)))
        self.assertFalse(run(bashd("ls")))
        self.assertTrue(
            run({"tool_name": "Read", "tool_input": {"file_path": "/x"}, "cwd": str(PROJ), "agent_type": MAPPER})
        )
        crash_proj = project()
        (crash_proj / "qa-out").mkdir()
        self.assertTrue(
            run({"tool_name": "Bash", "tool_input": {"command": "playwright-cli goto x"}, "cwd": str(crash_proj)})
        )
        recs = [json.loads(x) for x in (crash_proj / "qa-out/plugin-log.jsonl").read_text().splitlines()]
        self.assertTrue(
            any(
                r["type"] == "hook_error" and r["hook"] == "pretooluse" and r["error_type"] == "RuntimeError"
                for r in recs
            )
        )

    def test_nul_and_lone_surrogates(self):
        for cmd in [f"rm -rf {D} '\ud800'", f"rm -rf {D} 'a\x00b'"]:
            out = hook({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(PROJ)})
            self.assertIn("memory DB", out, cmd)  # the rule's own reason, not a guard error
        self.assertEqual(
            hook({"tool_name": "Bash", "tool_input": {"command": "ls '\ud800' 'a\x00b'"}, "cwd": str(PROJ)}), ""
        )

    def test_decoy_path_fields_and_malformed_input(self):
        d = lambda **kw: g.decide({"cwd": str(PROJ), **kw})
        self.assertIsNotNone(
            d(
                tool_name="Grep",
                agent_type=EX,
                tool_input={"file_path": "/tmp/decoy", "path": str(MAIN), "pattern": "x"},
            )
        )
        self.assertIsNotNone(
            d(tool_name="NotebookEdit", tool_input={"file_path": "/tmp/d.ipynb", "notebook_path": f"{MAIN}/x.ipynb"})
        )
        self.assertIsNotNone(d(tool_name="Read", tool_input={"file_path": "/tmp/decoy", "path": str(CK)}))
        self.assertIsNotNone(d(tool_name="Write", agent_type=EX, tool_input=["/srv/app/x.ts"]))
        self.assertIsNone(d(tool_name="Read", tool_input={"file_path": str(PROJ / "notes.txt")}))

    def test_speed(self):
        t = time.time()
        self.assertIsNotNone(
            bash("playwright-cli -s=qa eval \"'" + "a" * 99_000 + "'; location.href='https://evil.com'\"")
        )
        self.assertIsNotNone(bash("playwright-cli -s=qa eval \"'" + "a" * 200_000 + "'\""))
        self.assertLess(time.time() - t, 1)
        t = time.time()
        self.assertIsNone(bash("echo " + " ".join(f"$(date +%s{i})" for i in range(2000))))
        self.assertLess(time.time() - t, 1)
        t = time.time()
        for _ in range(50):
            bash(
                f"cd {RUN} && python3 {Q} annotate qa-out/r/evidence/BUG-1.png --box 1,1,1,1 --label x "
                f"&& playwright-cli -s=qa open --profile {MAIN} https://qa.traffic.example/",
                RUN,
            )
        self.assertLess((time.time() - t) / 50, 0.05)


class LibMirrorsGuard(Case):
    """The guard imports nothing from lib (it is the trust root and starts fast); lib's copy of its host rules, used by
    `session-check` and `doctor`, must decide exactly as it does, and lib's plugin-log writer (every other hook and
    qa-mem) must mask and mute like the guard's."""

    def test_lib_plugin_log_keeps_out_the_secrets_the_guard_masks(
        self,
    ):  # a Basic credential reached the log and digest
        from qa_sdet import plugin_log as pl

        cred = "dXNlcjpTM2NyM3RQYXNz"
        for text in (
            f"curl -H 'Authorization: Bearer {cred}' https://qa.example",
            f"playwright-cli -s=qa eval \"fetch(u, {{headers: {{'X-Auth': 'Basic {cred}='}}}})\"",
            f"qa-mem api-probe GET https://qa.example/api --header 'X-Upstream: Bearer {cred}'",
            f"JIRA_API_TOKEN={cred} acli jira workitem view X-1",
            f"acli jira workitem view X-1 --password {cred}",
            f"curl -u bob:{cred} https://qa.example",
            f"curl https://bob:{cred}@qa.example/",
            f"curl -H 'Cookie: sid={cred}' https://qa.example",
            f"curl -H 'X-Api-Key: {cred}' https://qa.example",
        ):
            with self.subTest(text=text):
                self.assertNotIn(cred, gl.mask(text))
                self.assertNotIn(cred, pl.scrub_text(text, 0))
        for prose in ("the basic search drops results", "Bearer of bad news on /closures"):  # user notes stay readable
            self.assertEqual(pl.scrub_text(prose, 0), prose)

    def test_lib_plugin_log_writes_and_mutes_like_the_guard(self):
        from qa_sdet import plugin_log as pl

        self.assertEqual((gl.PLUGIN_LOG_NAME, gl.DENIAL_LOG_LINES), (pl.LOG_NAME, pl.MAX_EVENTS))
        cfg = TMP / "mirror-plugin-log.json"
        for body in ("{}", '{"pluginLog": false}', '{"pluginLog": 0}', '{"pluginLog": "false"}', "[]", "not json"):
            cfg.write_text(body)
            with self.subTest(config=body), mock.patch.object(gp, "CONFIG", cfg):
                self.assertEqual(gl._plugin_log_enabled(), pl.enabled(cfg))

    def test_lib_reads_hosts_and_urls_by_the_guards_rules(self):  # session-check took user@host and a backslash host
        from qa_sdet import core

        lists = [
            ["qa.example.com"],
            ["QA.Example.com.", "*.api.example.com"],
            ["localhost"],
            [" ", 7],
            "qa.example.com",
        ]
        urls = [
            "https://qa.example.com/x",
            "http://sub.qa.example.com",
            "HTTPS://QA.EXAMPLE.COM",
            "https://evilqa.example.com/",
            "https://evil.com\\.qa.example.com/",
            "https://qa.example.com@evil.com/",
            "https://user@qa.example.com/",
            "https://a%2f.qa.example.com/",
            "javascript://qa.example.com/%0Aalert(1)",
            "ftp://qa.example.com/",
            "qa.example.com/x",
            "http://127.0.0.1:8765/",
            "http://[::1]:8765/",
            "https://api.example.com/v1",
            "http://[bad/",
            "https://qa.example.com./x",  # a trailing-dot host is the same host
            "https://qa.example.com/x?email=qa@example.com&next=https://other@x",  # @ and :// after the host
            "not a url",
            "",
        ]
        mirror = TMP / "mirror.json"
        for listed in lists:
            mirror.write_text(json.dumps({"allowedHosts": listed}))
            with mock.patch.object(gp, "CONFIG", mirror):
                hosts = gp.allowed_hosts()
            self.assertEqual(core.allowed_hosts({"allowedHosts": listed}), hosts, listed)
            for (
                url
            ) in urls:  # lib takes http(s) only (a QA URL to open or fetch); the guard also lets ws:// and about:blank
                with self.subTest(listed=listed, url=url):
                    want = gp.host_ok(url, hosts) and url.lower().startswith(("http://", "https://"))
                    self.assertEqual(core.url_allowed(url, hosts), want)


def runtime_imports(path):
    """Module names a file imports when it runs: imports under `if TYPE_CHECKING:` are for the type checker only."""
    names, todo = [], list(ast.parse(Path(path).read_text(encoding="utf-8")).body)
    while todo:
        node = todo.pop()
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            todo += node.orelse
            continue
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names += ["." * node.level + (node.module or a.name) for a in node.names]
        todo += [c for c in ast.iter_child_nodes(node) if isinstance(c, ast.stmt)]
    return names


class ImportBoundaries(unittest.TestCase):
    """make architecture: the guard is the trust root and imports nothing from lib; core and scrub, which every
    module uses, import no other qa_sdet module (no cycle can start there)."""

    def test_the_guard_imports_nothing_from_lib(self):
        files = [GUARD, *sorted(GUARD.parent.glob("guard_*.py"))]
        self.assertEqual(len(files), 7)  # the hook and its six modules
        for f in files:
            with self.subTest(file=f.name):
                found = runtime_imports(f)
                self.assertEqual([n for n in found if n.split(".")[0] == "qa_sdet" or n.startswith(".")], [])

    def test_core_and_scrub_import_no_other_qa_sdet_module(self):
        for name in ("core", "scrub"):
            with self.subTest(module=name):
                found = runtime_imports(ROOT / f"lib/qa_sdet/{name}.py")
                self.assertEqual([n for n in found if n.startswith(".") or n.startswith("qa_sdet")], [])

    def test_the_check_sees_imports_in_functions_and_skips_type_checking_blocks(self):
        src = TMP / "boundary_probe.py"
        src.write_text(
            "from . import ops\nTYPE_CHECKING = False\nif TYPE_CHECKING:\n    from . import contracts\n\n"
            "def f():\n    from qa_sdet import core\n"
        )
        self.assertEqual(sorted(runtime_imports(src)), [".ops", "qa_sdet"])


class KnownLimits(Case):
    """Given up on purpose (README Safety, known limits): pinned so a change here is a decision, not an accident."""

    def test_known_limits(self):
        self.table(
            [
                "$'\\x70laywright-cli' -s=qa goto https://evil.com",
                'p=play; $p"wright-cli" goto https://evil.com',
                f"cat {D}/{{profiles,x}}/main/Default/Cookies",
                f"find {D} | cpio -o > /tmp/x.cpio",
                "cat ~/.config/qa-sdet/qa-state.json",
            ],
            False,
            agents=(False,),
        )
        self.assertIsNone(tool("mcp__playwright__browser_navigate", url="https://evil.com"))
        self.table(
            [
                "playwright-cli -s=qa --raw eval \"fetch(u, {method: ['PO', 'ST'].join('')})\"",  # the raw-eval write deny is
                "playwright-cli -s=qa --raw eval \"const m = 'POST'; fetch(u, {method: m})\"",
            ],  # pattern-based
            False,
            agents=(False,),
        )
        cwd = project()
        (cwd / ".env").write_text("K=v")
        self.assertIsNone(tool("Grep", MAPPER, cwd, pattern="K", path=str(cwd), output_mode="content"))


if __name__ == "__main__":
    unittest.main()
