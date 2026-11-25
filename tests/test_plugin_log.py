"""Tests for lib/qa_sdet/plugin_log.py: JSONL capture, scrubbing, anonymization, digest export, cursor, show and clear."""
import json, os, time, unittest
from pathlib import Path

from support import Base, ROOT, core


class PluginLogCore(Base):
    def _mod(self):
        from qa_sdet import plugin_log
        return plugin_log

    def test_append_scrubs_secrets_stamps_version_and_caps(self):
        pl = self._mod()
        qa_out = self.proj / "qa-out"
        qa_out.mkdir()
        version = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"]
        ok = pl.append_event(
            qa_out, "denial",
            {"agent": "qa-explorer", "layer": "guard",
             "reason": "blocked with Bearer eyJhbGciOiJIUzI1NiJ9.payload123.sig123",
             "call": "curl -u bob:pw123 -H 'Authorization: Bearer tok9999999999999999' "
                     "'https://u:pw456@qa.corp.example/?token=s3cr3t' --password hiddenpw JIRA_API_TOKEN=tokxyz"},
        )
        self.assertTrue(ok)
        log = qa_out / pl.LOG_NAME
        rec = json.loads(log.read_text().strip())
        self.assertEqual((rec["v"], rec["type"], rec["agent"], rec["layer"]), (version, "denial", "qa-explorer", "guard"))
        self.assertRegex(rec["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        raw = log.read_text()
        for secret in ("eyJhbGciOiJIUzI1NiJ9", "payload123", "pw123", "tok9999999999999999", "pw456", "s3cr3t", "hiddenpw", "tokxyz"):
            self.assertNotIn(secret, raw, secret)

    def test_rotation_caps_jsonl_to_max_events(self):
        pl = self._mod()
        qa_out = self.proj / "qa-out"
        qa_out.mkdir()
        log = qa_out / pl.LOG_NAME
        seed = [json.dumps({"ts": "2026-11-20T00:00:00Z", "v": "0.4.0", "type": "nudge", "kind": "spin",
                            "detail": "x" * 120, "n": i}) for i in range(pl.MAX_EVENTS + 350)]
        log.write_text("\n".join(seed) + "\n")
        self.assertTrue(pl.append_event(qa_out, "nudge", {"kind": "stuck", "detail": "click e5"}))
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), pl.MAX_EVENTS)
        last = json.loads(lines[-1])
        self.assertEqual((last["type"], last["detail"]), ("nudge", "click e5"))

    def test_disabled_via_config_writes_nothing_and_refuses_manual_note(self):
        pl = self._mod()
        self.config(pluginLog=False)
        qa_out = self.proj / "qa-out"
        qa_out.mkdir()
        self.assertFalse(pl.append_event(qa_out, "nudge", {"kind": "spin"}))
        self.assertFalse((qa_out / pl.LOG_NAME).exists())
        with self.assertRaises(core.BadInput) as ctx:
            pl.add_note(self.proj, "annoying report layout")
        self.assertIn("pluginLog", str(ctx.exception))
        self.assertFalse((qa_out / pl.LOG_NAME).exists())

    def test_symlink_and_fifo_never_followed_or_raise(self):
        pl = self._mod()
        qa_out = self.proj / "qa-out"
        qa_out.mkdir()
        victim = self.t / "victim.txt"
        victim.write_text("keep\n")
        log = qa_out / pl.LOG_NAME
        log.symlink_to(victim)
        self.assertFalse(pl.append_event(qa_out, "nudge", {"kind": "spin"}))
        self.assertEqual(victim.read_text(), "keep\n")
        log.unlink()
        os.mkfifo(log)
        self.assertFalse(pl.append_event(qa_out, "nudge", {"kind": "spin"}))

    def test_manual_note_attaches_latest_run_context_and_validates(self):
        pl = self._mod()
        self.rundir("2026-11-25-PROJ-42", runs=[("incidents", {
            "run_id": "ticket-incidents-1", "mode": "ticket", "target": "/incidents/99", "status": "INCOMPLETE",
        })])
        rec = pl.add_note(self.proj, "Explorer retried disabled Save button 3 times", kind="nuisance", severity="med")
        self.assertEqual((rec["type"], rec["kind"], rec["severity"]), ("user_note", "nuisance", "med"))
        self.assertEqual(rec["run"], "2026-11-25-PROJ-42")
        self.assertEqual(rec["slug"], "incidents")
        self.assertEqual(rec["mode"], "ticket")
        self.assertEqual(rec["route"], "/incidents/:id")
        with self.assertRaises(core.BadInput):
            pl.add_note(self.proj, "bad kind", kind="rant")
        with self.assertRaises(core.BadInput):
            pl.add_note(self.proj, "bad sev", severity="urgent")
        with self.assertRaises(core.BadInput):
            pl.add_note(self.proj, "   ")


class PluginLogAnonymizerAndExport(Base):
    def _mod(self):
        from qa_sdet import plugin_log
        return plugin_log

    def test_anonymizer_strips_work_identifiers_but_keeps_structure_and_tech_tokens(self):
        pl = self._mod()
        self.config(
            app="TrafficControl",
            qaBaseUrl="https://qa.acme-corp.internal",
            allowedHosts=["qa.acme-corp.internal", "api.acme-corp.internal"],
            ssoHost="login.microsoftonline.com",
            jiraProject="TRAF",
            jiraKeyPrefix="TRAF",
            sourcePaths=["/home/alice/work/traffic-ui", "/home/alice/work/traffic-api"],
        )
        anon = pl.Anonymizer(self.proj, core.config())
        txt = anon.text(
            "TrafficControl failed on https://qa.acme-corp.internal/incidents/4521?owner=alice&tab=2 "
            "and https://api.acme-corp.internal/v1/items/550e8400-e29b-41d4-a716-446655440000 "
            "via login.microsoftonline.com for TRAF-1234 and OPS-99 (TC-1, AC-2, BUG-3, HTTP-500, UTF-8, SHA-256) "
            f"at {self.proj}/qa-out/2026-11-25-TRAF-1234 and /home/alice/work/traffic-ui/src/App.tsx "
            "contact alice@acme-corp.internal localhost:8765"
        )
        for leak in ("TrafficControl", "qa.acme-corp.internal", "api.acme-corp.internal", "login.microsoftonline.com",
                     "TRAF-1234", "OPS-99", "/home/alice", "alice@acme-corp.internal", "4521",
                     "550e8400-e29b-41d4-a716-446655440000", "owner=alice"):
            self.assertNotIn(leak, txt, f"leaked {leak!r} in: {txt}")
        for keep in ("<app>", "<host-1>", "<host-2>", "<sso-host>", "TICKET-1", "TICKET-2",
                     "TC-1", "AC-2", "BUG-3", "HTTP-500", "UTF-8", "SHA-256",
                     "<project>/qa-out/2026-11-25-TICKET-1", "<src-1>/src/App.tsx",
                     "/incidents/:id", "<email>", "localhost:8765"):
            self.assertIn(keep, txt, f"missing {keep!r} in: {txt}")

        cmd = anon.command(
            "playwright-cli -s=qa fill e12 'Secret Customer Name' && "
            "playwright-cli -s=qa eval \"document.querySelector('.corp-secret').textContent\" && "
            "acli jira workitem search --jql 'project = TRAF AND summary ~ \"payroll\"' --json && "
            "qa-mem dispatch /home/alice/proj/qa-out/r --route /incidents/42 --focus 'payroll totals' --api-writes"
        )
        for leak in ("Secret Customer Name", "corp-secret", "payroll", "/home/alice", "TRAF"):
            self.assertNotIn(leak, cmd, f"leaked {leak!r} in cmd: {cmd}")
        for keep in ("playwright-cli", "fill e12", "eval", "acli jira workitem search", "--jql", "qa-mem dispatch", "--api-writes"):
            self.assertIn(keep, cmd, f"missing {keep!r} in cmd: {cmd}")

    def test_export_dedup_run_health_cursor_and_non_clobbering_files(self):
        pl = self._mod()
        self.config(app="CorpApp", qaBaseUrl="https://qa.corp.example", allowedHosts=["qa.corp.example"], jiraProject="CORP")
        qa_out = self.proj / "qa-out"
        qa_out.mkdir()
        t1, t2, t3 = "2026-11-25T10:00:00Z", "2026-11-25T10:05:00Z", "2026-11-25T10:10:00Z"
        pl.append_event(qa_out, "user_note", {"kind": "bug", "severity": "high", "run": "2026-11-25-CORP-9",
                                              "route": "/zones/7", "text": "CORP-9 report missed https://qa.corp.example/zones/7"}, ts=t1)
        pl.append_event(qa_out, "denial", {"layer": "guard", "agent": "qa-explorer", "reason": "host not allowed",
                                           "call": "playwright-cli -s=qa goto https://evil.corp.example/x"}, ts=t1)
        pl.append_event(qa_out, "denial", {"layer": "guard", "agent": "qa-explorer", "reason": "host not allowed",
                                           "call": "playwright-cli -s=qa goto https://evil.corp.example/y"}, ts=t1)
        pl.append_event(qa_out, "nudge", {"agent": "qa-explorer", "kind": "stuck", "detail": "click e5", "count": 3}, ts=t2)
        pl.append_event(qa_out, "stop_bounce", {"agent": "qa-explorer", "kind": "verify_run",
                                                "items": ["TC-2 need `actual`", "missing requests-form.log"]}, ts=t2)
        pl.append_event(qa_out, "cli_error", {"cmd": "page-start", "exit_code": 1, "reason": "session expired"}, ts=t2)
        pl.append_event(qa_out, "hook_error", {"hook": "posttooluse", "error_type": "KeyError", "detail": "'slug'"}, ts=t2)
        pl.append_event(qa_out, "tool_fail", {"agent": "qa-explorer", "cmd": "playwright-cli -s=qa click e5",
                                              "error": "Timeout 30000ms exceeded"}, ts=t2)
        pl.append_event(qa_out, "run_summary", {
            "run": "2026-11-25-CORP-9", "slug": "zones", "mode": "ticket", "route": "/zones/7",
            "status": "INCOMPLETE", "stop_reason": "turn_limit", "turns": 38, "commands": 24, "budget": 0,
            "duration_s": 140, "build_known": False, "bugs_found": 1, "audit_issues": 2, "missing_run_file": False,
            "cases": {"pass": 2, "fail": 1, "blocked": 0, "not_run": 1, "question": 0},
        }, ts=t2)

        # Preview does not write a file or advance the cursor
        prev = pl.export_digest(self.proj, preview=True, day="2026-11-25")
        self.assertIsNone(prev["path"])
        self.assertEqual(prev["events"], 9)
        self.assertFalse((qa_out / pl.CURSOR_NAME).exists())
        md = prev["markdown"]
        self.assertIn("×2", md)
        self.assertIn("## User Notes (1)", md)
        self.assertIn("TICKET-1", md)
        self.assertNotIn("CORP-9", md)
        self.assertNotIn("qa.corp.example", md)
        self.assertIn("turn_limit", md)
        self.assertIn("unknown", md)

        # Real export writes dated file and advances cursor
        first = pl.export_digest(self.proj, day="2026-11-25")
        self.assertEqual(first["path"], qa_out / "plugin-digest-2026-11-25.md")
        self.assertTrue(first["path"].is_file())
        self.assertTrue((qa_out / pl.CURSOR_NAME).is_file())

        # Immediate re-export finds 0 new events and writes no file
        empty = pl.export_digest(self.proj, day="2026-11-25")
        self.assertEqual(empty["events"], 0)
        self.assertIsNone(empty["path"])

        # Same-second event appended AFTER cursor must still be picked up; dated file becomes -2.md
        pl.append_event(qa_out, "user_note", {"kind": "idea", "text": "add export shortcut"}, ts=t2)
        pl.append_event(qa_out, "nudge", {"agent": "qa-explorer", "kind": "spin", "detail": "8 calls"}, ts=t3)
        second = pl.export_digest(self.proj, day="2026-11-25")
        self.assertEqual(second["events"], 2)
        self.assertEqual(second["path"], qa_out / "plugin-digest-2026-11-25-2.md")
        self.assertTrue(first["path"].is_file())  # not clobbered
        self.assertIn("add export shortcut", second["markdown"])

        # --all includes everything again; --since filters by timestamp
        all_res = pl.export_digest(self.proj, all_events=True, preview=True)
        self.assertEqual(all_res["events"], 11)
        since_res = pl.export_digest(self.proj, since=t3, preview=True)
        self.assertEqual(since_res["events"], 1)

    def test_show_and_clear(self):
        pl = self._mod()
        qa_out = self.proj / "qa-out"
        qa_out.mkdir()
        pl.append_event(qa_out, "user_note", {"kind": "nuisance", "text": "too verbose"}, ts="2026-11-25T10:00:00Z")
        pl.append_event(qa_out, "nudge", {"agent": "qa-explorer", "kind": "spin", "detail": "8 calls"}, ts="2026-11-25T10:01:00Z")
        digest = pl.export_digest(self.proj, day="2026-11-25")["path"]
        out = pl.show_log(self.proj, limit=10, event_type="user_note")
        self.assertIn("too verbose", out)
        self.assertNotIn("spin", out)
        pl.clear_log(self.proj)
        self.assertFalse((qa_out / pl.LOG_NAME).exists())
        self.assertFalse((qa_out / pl.CURSOR_NAME).exists())
        self.assertTrue(digest.is_file())  # exported digest preserved


class PluginLogCli(Base):
    def test_cli_note_show_export_clear_and_cli_error_capture(self):
        from unittest import mock
        from support import cli
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.proj)}):
            # Project has no qa-out yet: failing CLI command must NOT create qa-out/
            self.assertEqual(cli("stale", "none")[0], 2)
            self.assertFalse((self.proj / "qa-out").exists())

            # Manual note creates qa-out/ and records a user_note
            code, out, err = cli("plugin-log", "note", "--kind", "nuisance", "--severity", "low",
                                 "--project", str(self.proj), "report hides counts")
            self.assertEqual((code, err), (0, ""))
            self.assertIn("logged [nuisance/low]", out)

            # Now qa-out exists: a failing qa-mem command logs cli_error; --help does NOT
            self.assertEqual(cli("stale", "none")[0], 2)
            self.assertEqual(cli("stale", "--help")[0], 2)
            recs = [json.loads(x) for x in (self.proj / "qa-out/plugin-log.jsonl").read_text().splitlines()]
            cli_errs = [r for r in recs if r["type"] == "cli_error"]
            self.assertEqual(len(cli_errs), 1)
            self.assertEqual((cli_errs[0]["cmd"], cli_errs[0]["exit_code"]), ("stale", 2))

            # show, export --preview, export, clear
            code, out, _ = cli("plugin-log", "show", "--project", str(self.proj), "--limit", "5")
            self.assertEqual(code, 0)
            self.assertIn("report hides counts", out)

            code, out, _ = cli("plugin-log", "export", "--project", str(self.proj), "--preview")
            self.assertEqual(code, 0)
            self.assertIn("# qa-sdet Plugin Digest", out)
            self.assertIn("report hides counts", out)

            code, out, _ = cli("plugin-log", "export", "--project", str(self.proj))
            self.assertEqual(code, 0)
            self.assertIn("exported 2 event(s)", out)

            code, out, _ = cli("plugin-log", "export", "--project", str(self.proj))
            self.assertEqual(code, 0)
            self.assertIn("no new plugin-log events", out)

            code, out, _ = cli("plugin-log", "clear", "--project", str(self.proj))
            self.assertEqual(code, 0)
            self.assertIn("cleared", out)

            # Bad combinations fail with usage/error exit 2
            self.assertEqual(cli("plugin-log", "export", "--all", "--since", "7d", "--project", str(self.proj))[0], 2)
            self.assertEqual(cli("plugin-log", "show", "--limit", "0", "--project", str(self.proj))[0], 2)


if __name__ == "__main__":
    unittest.main()
