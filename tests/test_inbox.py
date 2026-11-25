"""`qa-mem inbox`: the open Jira tickets, newest first, new ones marked."""
import json, stat, unittest
from unittest import mock

from support import Base, cli, core
from qa_sdet import report


class Inbox(Base):
    """`qa-mem inbox` reads acli's JSON, writes qa-out/INBOX.md and remembers which tickets it has shown."""

    ACLI = [{"key": "PROJ-5", "fields": {"summary": "Old one", "status": {"name": "In Progress"},
                                           "priority": {"name": "High"}, "updated": "2026-11-20T10:00:00.000+0000"}},
            {"key": "PROJ-7", "fields": {"summary": "Newer | pipe", "status": {"name": "To Do"},
                                           "priority": {"name": "Low"}, "updated": "2026-11-22T10:00:00.000+0000"}}]

    def test_shapes(self):
        flat = [{"key": "ops-1", "summary": "Flat", "status": "Open", "priority": "P2", "updated": "2026-11-01"}]
        for data in (self.ACLI, {"issues": self.ACLI}, {"workItems": self.ACLI}, {"values": self.ACLI}):
            self.assertEqual([x["key"] for x in report.inbox_items(data)], ["PROJ-5", "PROJ-7"])
        self.assertEqual(report.inbox_items(flat + [{"nokey": 1}, "junk"]),
                         [{"key": "OPS-1", "summary": "Flat", "status": "Open", "priority": "P2", "updated": "2026-11-01"}])
        for bad in ({"x": 1}, "text", 5):
            with self.assertRaises(core.BadInput):
                report.inbox_items(bad)

    def test_new_tracking_persists(self):
        self.config(jiraKeyPrefix="PROJ")
        code, out, _ = cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI))
        self.assertEqual(code, 0)
        rows = [ln for ln in out.splitlines() if ln.startswith("| ")][1:]
        self.assertIn("| New | Key | Summary | Status | Priority | Updated |", out)
        self.assertEqual(rows, ["| NEW | PROJ-7 | Newer \\| pipe | To Do | Low | 2026-11-22 |",
                                "| NEW | PROJ-5 | Old one | In Progress | High | 2026-11-20 |"])
        self.assertEqual(out.splitlines()[-1], "Next: /qa-sdet:qa-ticket 7 5")
        self.assertEqual((self.proj / "qa-out/INBOX.md").read_text(), out)
        self.assertTrue(out.startswith("# Jira inbox\n"))
        wrapped = {"issues": self.ACLI + [{"key": "OPS-9", "fields": {"summary": "Other", "updated": "2026-11-01"}}]}
        code, out, _ = cli("inbox", str(self.proj), stdin=json.dumps(wrapped))  # an object with `issues` works too
        rows = [ln for ln in out.splitlines() if ln.startswith("| ")][1:]
        self.assertEqual([r.split(" | ")[0:2] for r in rows], [["| NEW", "OPS-9"], ["| ", "PROJ-7"], ["| ", "PROJ-5"]])
        self.assertEqual(out.splitlines()[-1], "Next: /qa-sdet:qa-ticket OPS-9")
        code, out, _ = cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI))
        self.assertEqual(out.splitlines()[-1], "Next: /qa-sdet:qa-ticket 7 5")  # nothing new: top 3
        self.assertNotIn("| NEW", out)

    def test_bad_input(self):
        self.assertEqual(cli("inbox", str(self.proj), stdin="not json")[0], 2)
        code, _, err = cli("inbox", str(self.proj), stdin='[{"id": "10001", "self": "https://x/issue/10001"}]')
        self.assertEqual((code, "none of the 1 items has a Jira key" in err), (2, True))
        self.assertEqual(cli("inbox", str(self.proj), stdin='{"x": 1}')[0], 2)
        self.assertEqual(cli("inbox")[0], 2)
        self.assertFalse((self.proj / "qa-out").exists())
        code, out, _ = cli("inbox", str(self.proj), stdin="[]")
        self.assertIn("No open tickets", out)
        self.assertEqual(out.splitlines()[-1], "Next: nothing to test")
        code, out, _ = cli("inbox", str(self.proj), stdin='[{"key": "AB-1", "summary": "bad \\ud800 char"}]')  # was: UnicodeEncodeError
        self.assertEqual((code, "AB-1" in out), (0, True))

    def test_duplicates_typo_project_deep_garbage_big_inbox(self):
        code, out, _ = cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI + self.ACLI[:1]))
        self.assertIn("2 open, 2 new", out)  # PROJ-5 listed twice is one ticket
        self.assertEqual(sum(ln.count("PROJ-5") for ln in out.splitlines() if ln.startswith("| ")), 1)
        self.assertEqual(cli("inbox", str(self.t / "typo/p"), stdin="[]")[0], 2)
        self.assertFalse((self.t / "typo").exists())
        code, _, err = cli("inbox", str(self.proj), stdin="[" * 100000)
        self.assertEqual(code, 2)
        self.assertIn("can't read Jira JSON", err)
        big = json.dumps([{"key": f"ZZ-{i}"} for i in range(6000)])
        self.assertIn("6000 open, 6000 new", cli("inbox", str(self.proj), stdin=big)[1])
        self.assertIn("6000 open, 0 new", cli("inbox", str(self.proj), stdin=big)[1])  # was 1000 new forever

    def test_failed_write_keeps_tickets_new(self):  # keys were remembered before INBOX.md was written
        with mock.patch.object(core, "_write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI))
        self.assertIn("2 open, 2 new", cli("inbox", str(self.proj), stdin=json.dumps(self.ACLI))[1])

    def test_planted_temp_link_is_not_written_through(self):  # _write opened .INBOX.md.tmp following links
        victim = self.t / "victim"
        victim.write_text("keep")
        (self.proj / "qa-out").mkdir()
        for name in (".INBOX.md.tmp", ".README.md.tmp", ".REPORT.md.tmp"):
            (self.proj / "qa-out" / name).symlink_to(victim)
        self.assertEqual(cli("inbox", str(self.proj), stdin="[]")[0], 0)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse((self.proj / "qa-out/INBOX.md").is_symlink())
        self.assertEqual(stat.S_IMODE((self.proj / "qa-out/INBOX.md").stat().st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in (self.proj / "qa-out").iterdir() if ".tmp" in p.name and
                                not p.is_symlink()), [])

    def test_overlapping_inbox_runs_keep_each_others_keys(self):
        a, b = core.connect(), core.connect()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        real = core._write
        def b_runs_meanwhile(path, text):
            core._write = real
            report.inbox(b, self.proj, [{"key": "OPS-2"}])
            real(path, text)
        with mock.patch.object(core, "_write", b_runs_meanwhile):
            report.inbox(a, self.proj, [{"key": "OPS-1"}])
        self.assertEqual(sorted(json.loads(core.meta(a, "inbox_seen"))), ["OPS-1", "OPS-2"])

    def test_status_change_last_qa_column_and_empty_updated_omitted(self):
        from support import PLAN, RUN_FORM
        self.config(jiraKeyPrefix="PROJ")
        # Legacy list in inbox_seen migrates cleanly without marking PROJ-5 NEW
        con = core.connect()
        try:
            with con:
                core.set_meta(con, "inbox_seen", json.dumps(["PROJ-5"]))
        finally:
            con.close()
        # Local runs: PROJ-1 failed, PROJ-2 passed (use PLAN1-style single runs)
        p_pass = {"kind": "ticket", "tickets": [{"key": "PROJ-5"}],
                  "cases": [{"id": "TC-1", "ticket": "PROJ-5", "route": "/form"}]}
        r_pass = {"target": "/form", "build": "1.0", "status": "COMPLETED", "cases": [{"id": "TC-1", "status": "PASS"}]}
        self.rundir("2026-11-23-PROJ-5", plan=p_pass, runs=[("form", r_pass)])
        p_fail = {"kind": "ticket", "tickets": [{"key": "PROJ-7"}],
                  "cases": [{"id": "TC-1", "ticket": "PROJ-7", "route": "/form"}]}
        r_fail = {"target": "/form", "build": "1.0", "status": "COMPLETED", "cases": [{"id": "TC-1", "status": "FAIL"}]}
        self.rundir("2026-11-23-PROJ-7", plan=p_fail, runs=[("form", r_fail)])
        # Items without `updated` (as real acli returns): Updated column omitted; Last QA shown
        items = [{"key": "PROJ-5", "fields": {"summary": "Passed one", "status": {"name": "In QA"}, "priority": {"name": "Low"}}},
                 {"key": "PROJ-7", "fields": {"summary": "Failed one", "status": {"name": "In Progress"}, "priority": {"name": "High"}}}]
        code, out, _ = cli("inbox", str(self.proj), stdin=json.dumps(items))
        self.assertEqual(code, 0)
        self.assertNotIn("| Updated |", out)
        self.assertIn("| New | Key | Summary | Status | Priority | Last QA |", out)
        self.assertIn("|  | PROJ-5 | Passed one | In QA | Low | ✅ PASS |", out)
        self.assertIn("| NEW | PROJ-7 | Failed one | In Progress | High | ❌ FAIL |", out)
        self.assertEqual(out.splitlines()[-1], "Next: /qa-sdet:qa-ticket 7")
        # Next check: PROJ-7 status changes In Progress -> Ready for QA -> flagged CHANGED; unchanged PASS PROJ-5 skipped
        items2 = [items[0], {"key": "PROJ-7", "fields": {"summary": "Failed one", "status": {"name": "Ready for QA"}, "priority": {"name": "High"}}}]
        code, out2, _ = cli("inbox", str(self.proj), stdin=json.dumps(items2))
        self.assertEqual(code, 0)
        self.assertIn("| CHANGED | PROJ-7 |", out2)
        self.assertEqual(out2.splitlines()[-1], "Next: /qa-sdet:qa-ticket 7")
        # Next check: nothing new or changed -> Next skips ✅ PASS PROJ-5 and picks PROJ-7
        code, out3, _ = cli("inbox", str(self.proj), stdin=json.dumps(items2))
        self.assertEqual(code, 0)
        self.assertEqual(out3.splitlines()[-1], "Next: /qa-sdet:qa-ticket 7")


class Prep(Base):
    """`qa-mem prep`: single deterministic read-only ticket+plan+rules+memory context bundle."""

    def test_prep_bundles_acli_ac_field_comments_rules_plan_and_scrubs(self):
        import subprocess
        from support import done, PLAN
        self.config(jiraProject="PROJ", jiraKeyPrefix="PROJ", jiraAcField="customfield_10042", qaBaseUrl="https://qa.example")
        (self.proj / "qa-rules.md").write_text("never click Confirm\n")
        self.rundir("2026-11-23-plan-PROJ-1", plan={**PLAN, "kind": "plan", "tickets": [PLAN["tickets"][0]],
                                                    "cases": PLAN["cases"][:2]})
        view_json = json.dumps({"key": "PROJ-1", "fields": {"summary": "Speed", "customfield_10042": "Max 130"}})
        comm_json = json.dumps({"comments": [{"author": {"displayName": "Dev"}, "created": "2026-11-24",
                                              "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                                                  {"type": "text", "text": "See token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk"}]}]}}]})
        seq = [done(0), done(0, out=view_json), done(0, out=comm_json)]
        with mock.patch("shutil.which", return_value="/usr/bin/acli"), \
                mock.patch.object(subprocess, "run", side_effect=seq) as mrun:
            code, out, err = cli("prep", str(self.proj), "1")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["keys"], ["PROJ-1"])
        self.assertTrue(data["acli"])
        self.assertIn("customfield_10042", mrun.call_args_list[1][0][0][mrun.call_args_list[1][0][0].index("--fields") + 1])
        self.assertEqual(data["tickets"][0]["fields"]["customfield_10042"], "Max 130")
        self.assertEqual(data["tickets"][0]["comments"][0]["body"], "See token [REDACTED-JWT]")
        self.assertNotIn("eyJhbGci", out)
        self.assertIn("[REDACTED-JWT]", out)
        self.assertIn("never click Confirm", data["rules"])
        self.assertIsNotNone(data["plan_state"]["reusable_plan"])

    def test_prep_fallback_without_acli_and_custom_field_retry(self):
        import subprocess
        from support import done
        self.config(jiraKeyPrefix="PROJ", jiraAcField="customfield_99999")
        with mock.patch("shutil.which", return_value=None):
            code, out, err = cli("prep", str(self.proj), "12")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual((data["keys"], data["acli"], data["tickets"]), (["PROJ-12"], False, []))
        # Retry without custom field when acli rejects it
        seq = [done(0), done(1, err="unknown field customfield_99999"),
               done(0, out=json.dumps({"key": "PROJ-12", "fields": {"summary": "Ok"}})), done(0, out="[]")]
        with mock.patch("shutil.which", return_value="/usr/bin/acli"), \
                mock.patch.object(subprocess, "run", side_effect=seq) as mrun:
            code, out, err = cli("prep", str(self.proj), "12")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["tickets"][0]["fields"]["summary"], "Ok")
        self.assertNotIn("customfield_99999", " ".join(mrun.call_args_list[2][0][0]))

    def test_prep_flattens_adf_description_and_ac_field(self):
        """Jira Cloud returns description and custom fields as ADF trees: prep gives them as text, words not glued."""
        import subprocess
        from support import done
        self.config(jiraKeyPrefix="PROJ", jiraAcField="customfield_10042")
        para = lambda *nodes: {"type": "paragraph", "content": list(nodes)}
        txt = lambda t: {"type": "text", "text": t}
        desc = {"type": "doc", "content": [
            para(txt("Owner "), {"type": "mention", "attrs": {"id": "1", "text": "@Ann"}}, txt(" see "),
                 {"type": "inlineCard", "attrs": {"url": "https://wiki.example/x"}}),
            para({"type": "status", "attrs": {"text": "IN REVIEW"}}, txt(" due "),
                 {"type": "date", "attrs": {"timestamp": "1795478400000"}}),
            {"type": "table", "content": [{"type": "tableRow", "content": [
                {"type": "tableHeader", "content": [para(txt("Field"))]},
                {"type": "tableCell", "content": [para(txt("Max"))]}]}]}]}
        acs = {"type": "doc", "content": [{"type": "bulletList", "content": [
            {"type": "listItem", "content": [para(txt("Saves 130"))]},
            {"type": "listItem", "content": [para(txt("Rejects 131"))]}]}]}
        view = json.dumps({"key": "PROJ-1", "fields": {"summary": "Speed", "description": desc, "customfield_10042": acs}})
        with mock.patch("shutil.which", return_value="/usr/bin/acli"), \
                mock.patch.object(subprocess, "run", side_effect=[done(0), done(0, out=view), done(0, out="[]")]):
            code, out, err = cli("prep", str(self.proj), "1")
        self.assertEqual((code, err), (0, ""))
        fields = json.loads(out)["tickets"][0]["fields"]
        self.assertEqual(fields["description"], "Owner @Ann see https://wiki.example/x\nIN REVIEW due 2026-11-24\nField | Max")
        self.assertEqual(fields["customfield_10042"], "- Saves 130\n- Rejects 131")

    def test_prep_context_matches_the_ticket_text(self):
        """Not 70 routes and every bug: the routes, facts and bugs the ticket's words point at, status kept."""
        import subprocess
        from support import done, run
        from qa_sdet import core, memory
        self.config(jiraKeyPrefix="PROJ")
        con = core.connect()
        r = lambda route, **page: {**run(**page), "target": route, "pages": [{"route": route, **page}]}
        memory.ingest(con, {**r("/zones", facts=[{"kind": "rule", "text": "Corridor dates display in UTC"}]),
                            "bugs": [{"route": "/zones", "title": "Date filter drops the last day", "symptom": "filter end date excluded"}]})
        memory.ingest(con, {**r("/form", facts=[{"kind": "rule", "text": "Speed max 130"}]),
                            "bugs": [{"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved"}]})
        con.close()
        view = json.dumps({"key": "PROJ-1", "fields": {"summary": "Date filter timezone rollover on speed zones",
                                                        "description": "On /zones the end date filter is off by one day."}})
        with mock.patch("shutil.which", return_value="/usr/bin/acli"), \
                mock.patch.object(subprocess, "run", side_effect=[done(0), done(0, out=view), done(0, out="[]")]):
            code, out, err = cli("prep", str(self.proj), "1")
        self.assertEqual((code, err), (0, ""))
        ctx = json.loads(out)["context"]
        self.assertEqual(ctx["candidate_routes"][0]["route"], "/zones")
        self.assertTrue(any("Corridor dates" in x for x in ctx["relevant_facts"]))
        self.assertTrue(any("OPEN" in x and "Date filter drops" in x for x in ctx["relevant_bugs"]))
        self.assertFalse(any("Speed accepts 131" in x for x in ctx["relevant_bugs"]))
        self.assertIn("bugs_open", json.loads(out))  # the full lists stay for callers that read them

    def test_prep_fingerprints_tickets_and_a_changed_ticket_is_fully_retested(self):
        """Its requirements changed in Jira since the run: its earlier passes were checked against the old ones."""
        import subprocess
        from support import done, PLAN
        self.config(jiraKeyPrefix="PROJ")
        plan = {**PLAN, "tickets": [{**PLAN["tickets"][0], "source_snapshot": {"fields_sha1": "old", "captured_at": "2026-11-20T00:00:00Z"}},
                                     {**PLAN["tickets"][1]}]}
        runs = [("form", {"target": "/form", "status": "COMPLETED", "build": "1", "cases": [{"id": "TC-1", "status": "PASS"},
                                                                                             {"id": "TC-2", "status": "FAIL"}]})]
        self.rundir("2026-11-23-PROJ-1+PROJ-2", plan=plan, runs=runs)
        view = json.dumps({"key": "PROJ-1", "fields": {"summary": "Speed limit form", "description": "Max is now 120"}})
        view2 = json.dumps({"key": "PROJ-2", "fields": {"summary": "Incident list"}})
        with mock.patch("shutil.which", return_value="/usr/bin/acli"), \
                mock.patch.object(subprocess, "run", side_effect=[done(0), done(0, out=view), done(0, out="[]"),
                                                                  done(0, out=view2), done(0, out="[]")]):
            code, out, err = cli("prep", str(self.proj), "1", "2")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertRegex(data["tickets"][0]["source_snapshot"]["fields_sha1"], r"^[0-9a-f]{40}$")
        drift = data["plan_state"]["drift"]
        self.assertEqual(list(drift), ["PROJ-1"])  # PROJ-2 had no snapshot: nothing to compare
        self.assertEqual(drift["PROJ-1"]["prior_captured_at"], "2026-11-20T00:00:00Z")
        lr = data["plan_state"]["latest_run"]
        self.assertIn("TC-1", [c["id"] for c in lr["retest_cases"]])  # its pass is rerun
        self.assertNotIn("TC-1", [c["id"] for c in lr["passed_cases"]])


if __name__ == "__main__":
    unittest.main()
