"""The Stop and SubagentStop hooks (`qa-mem ingest-pending`) and the hooks.json wiring."""
import json, os, re, subprocess, sys, unittest
from pathlib import Path

from support import Base, runfile, run, ROOT, memory


class Hooks(Base):
    """hooks.json, and `qa-mem ingest-pending` as the Stop hook runs it."""

    def test_hooks_config_matches_plugin_and_agent_names(self):
        """SubagentStop matchers are exact agent types (<plugin>:<agent>), verified with a live probe."""
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["name"]
        agent = re.search(r"^name:\s*(\S+)", (ROOT / "agents/qa-explorer.md").read_text(), re.M).group(1)
        self.assertIn(f"{plugin}:{agent}", hooks["SubagentStop"][0]["matcher"].split("|"))
        # every tool: MCP names vary in case and server name, and the explorer's tool allowlist is enforced here
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "*")
        self.assertIn("/hooks/pretooluse.py", hooks["PreToolUse"][0]["hooks"][0]["command"])  # guard.py, bytecode cached
        for event in ("SubagentStop", "Stop"):
            self.assertIn('"$CLAUDE_PROJECT_DIR/qa-out"', hooks[event][0]["hooks"][0]["command"])

    def test_stop_hook_command_skips_python_without_qa_out(self):  # no Python start in unrelated projects
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        cmd = hooks["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(cmd, hooks["SubagentStop"][0]["hooks"][0]["command"])
        base = Path(self.tmp.name)
        proj, other = self.proj, base / "other"
        other.mkdir()
        stub = base / "bin"  # a python3 that notes it was started (the absence of a db alone can't tell)
        stub.mkdir()
        (stub / "python3").write_text(f'#!/bin/sh\ntouch "{base}/python-started"\nexec "{sys.executable}" "$@"\n')
        (stub / "python3").chmod(0o755)
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT), "CLAUDE_PROJECT_DIR": str(proj), "QA_MEM_DB": str(base / "db/x.db"),
               "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}"}
        sh = lambda cwd: subprocess.run(["bash", "-c", cmd], cwd=cwd, env=env, input="{}", capture_output=True, text=True)
        self.assertEqual(sh(other).returncode, 0)
        self.assertFalse((base / "db").exists())
        self.assertFalse((base / "python-started").exists())
        (proj / "qa-out/a").mkdir(parents=True)  # the project's folder, reached from another cwd
        runfile(proj / "qa-out/a").write_text(json.dumps(run(facts=[{"kind": "note", "text": "hi"}])))
        self.assertEqual(sh(other).returncode, 0)
        self.assertTrue((proj / "qa-out/a/.data/run-x.json.ingested").exists())
        (other / "qa-out/b").mkdir(parents=True)  # the cwd's folder alone
        runfile(other / "qa-out/b").write_text("{not json")
        r = sh(other)
        self.assertEqual(r.returncode, 2, r.stderr)  # the agent still hears about a run JSON it must fix
        self.assertTrue((other / "qa-out/b/.data/run-x.json.error").exists())

    def test_hook_in_unrelated_project_creates_no_db(self):
        os.environ["QA_MEM_DB"] = str(Path(self.tmp.name) / "fresh" / "x.db")
        self.assertEqual(memory.ingest_pending(Path(self.tmp.name) / "nope"), 0)
        self.assertFalse((Path(self.tmp.name) / "fresh").exists())

    def test_parked_run_is_shown_to_the_agent_once(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        runfile(out).write_text(json.dumps({"run_id": "r", "build": "1", "pages": [{"route": "/x", "facts": "oops"}]}))
        env = dict(os.environ, QA_MEM_DB=str(Path(self.tmp.name) / "hook.db"))
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", str(out.parent)],
                           input='{"stop_hook_active": false}', capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("could not save this", r.stderr)
        self.assertIn(str(ROOT / "references" / "run-json.md"), r.stderr)  # absolute, the project has no references/
        self.assertIn("did not write", r.stderr)  # a sibling explorer's parked file is not this agent's to fix
        runfile(out).write_text(json.dumps({"run_id": "r2", "build": "1", "pages": [{"route": "/x", "facts": 5}]}))
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", str(out.parent)],
                           input='{"stop_hook_active": true}', capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)  # already blocked once this turn: no loop

    def test_hook_entrypoint_never_fails(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", "/nonexistent"],
                           capture_output=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
