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


if __name__ == "__main__":
    unittest.main()
