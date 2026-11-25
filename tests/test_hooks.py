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

    def test_stop_hook_ignores_unset_project_dir(self):
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        cmd = hooks["Stop"][0]["hooks"][0]["command"]
        other = Path(self.tmp.name) / "empty-cwd"
        other.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
        # Check with trace that unset CLAUDE_PROJECT_DIR never tests /qa-out
        r = subprocess.run(["bash", "-x", "-c", cmd], cwd=other, env=env, input="{}", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("/qa-out", r.stderr)

    def post(self, data, env=None):
        """Run the PostToolUse hook as Claude Code does: JSON on stdin, from the project folder."""
        r = subprocess.run([sys.executable, str(ROOT / "hooks/posttooluse.py")], input=json.dumps(data), capture_output=True,
                           text=True, cwd=self.proj, env={**os.environ, **(env or {})})
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        if not r.stdout.strip():
            return ""
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], data.get("hook_event_name") or "PostToolUse")
        return out["additionalContext"]

    def test_post_tool_use_wiring(self):
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        entry = hooks["PostToolUse"][0]
        self.assertEqual(entry["matcher"], "Bash|Write")
        self.assertIn("/hooks/posttooluse.py", entry["hooks"][0]["command"])
        self.assertIn('[ -d qa-out ]', entry["hooks"][0]["command"])  # no Python start in unrelated projects
        failed = hooks["PostToolUseFailure"][0]  # a failed Bash call spends budget too, and fires only this event
        self.assertEqual((failed["matcher"], failed["hooks"][0]["command"]), ("Bash", entry["hooks"][0]["command"]))

    def test_explorer_is_warned_once_before_the_budget_runs_out(self):
        """Step 9 deletes test data through the UI, which the guard refuses once the budget is used up: warn first."""
        import time
        (self.proj / "qa-out").mkdir(exist_ok=True)
        marker = self.proj / "qa-out/.active"
        call = {"tool_name": "Bash", "agent_type": "qa-sdet:qa-explorer", "cwd": str(self.proj),
                "tool_input": {"command": "playwright-cli -s=qa click e1"}, "tool_response": {}}
        marker.write_text(json.dumps({"expires": time.time() + 3600, "budget": 20, "used": 10}))
        self.assertEqual(self.post(call), "")
        marker.write_text(json.dumps({"expires": time.time() + 3600, "budget": 20, "used": 17}))
        self.assertEqual(self.post({**call, "agent_type": ""}), "")  # the main session is no explorer
        note = self.post(call)
        self.assertIn("3 browser commands left", note)
        self.assertIn("qa-sdet-", note)
        self.assertEqual(self.post(call), "")  # once per run
        marker.write_text(json.dumps({"expires": time.time() + 3601, "budget": 20, "used": 20}))  # a new run, a failed call
        self.assertIn("0 browser commands left", self.post({**call, "hook_event_name": "PostToolUseFailure", "error": "Exit code 1"}))
        marker.write_text(json.dumps({"expires": time.time() + 7200, "deadline": time.time() + 90}))  # a new run, by time
        self.assertIn("left", self.post(call))
        marker.unlink()
        self.assertEqual(self.post(call), "")

    def bad_map(self, n):
        init = self.proj / "qa-out/init"
        init.mkdir(parents=True, exist_ok=True)
        f = init / f"code-map-frontend-{n}.json"
        f.write_text('{"area": "frontend", "scan_id": "missing"}')
        return f

    def test_a_mapper_hears_about_its_own_code_map_when_it_writes_it(self):
        env = {"QA_MEM_DB": str(Path(self.tmp.name) / "hook.db")}
        sibling, mine = self.bad_map(1), self.bad_map(2)
        note = self.post({"tool_name": "Write", "agent_type": "qa-sdet:qa-mapper", "cwd": str(self.proj),
                          "tool_input": {"file_path": str(mine), "content": "x"}}, env)
        self.assertIn(mine.name, note)
        self.assertNotIn(sibling.name, note)  # a sibling's file is the sibling's to fix
        self.assertTrue(mine.with_name(mine.name + ".error").exists())

    def test_a_sibling_mappers_stop_no_longer_takes_the_authors_feedback(self):
        env = dict(os.environ, QA_MEM_DB=str(Path(self.tmp.name) / "hook.db"))
        f1, f2 = self.bad_map(1), self.bad_map(2)
        stop = lambda event, agent="qa-sdet:qa-mapper": subprocess.run(
            [sys.executable, str(ROOT / "bin/qa-mem"), "ingest-pending", str(self.proj / "qa-out")], capture_output=True,
            text=True, env=env, input=json.dumps({"hook_event_name": event, "agent_type": agent, "stop_hook_active": False}))
        first = stop("SubagentStop")  # the first mapper to stop ingests both files
        self.assertEqual(first.returncode, 2)
        later = stop("SubagentStop")  # the author of the other file stops next: it still hears about it
        self.assertEqual(later.returncode, 2, later.stderr)
        self.assertIn(f1.name, later.stderr)
        self.assertIn(f2.name, later.stderr)
        self.assertEqual(stop("Stop", "").returncode, 0)  # the main session isn't nagged on every turn


if __name__ == "__main__":
    unittest.main()
