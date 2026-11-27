"""Stored bugs (qa_sdet.ingest, memory): what a run's bugs become, `bug-status`, `bug-sync` and `bug-match`."""

import contextlib
import io
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from support import MemBase, core, run
from support import cli as qa_mem

from qa_sdet import cards, cli, ingest, memory, scrub


class Bugs(MemBase):
    """How a bug in a run becomes one record: dedup, hash, sightings, status."""

    def test_bug_dedup_increments_seen_count(self):
        bug = {
            "route": "/form",
            "title": "Speed accepts 999",
            "symptom": "speed accepts 999",
            "severity": "HIGH",
            "classification": "CONFIRMED",
        }
        ingest.ingest(self.con, run(bugs=[bug]))
        ingest.ingest(self.con, run(bugs=[{**bug, "symptom": "Speed  accepts 999!"}]))
        rows = self.con.execute("SELECT seen_count FROM bugs").fetchall()
        self.assertEqual([r[0] for r in rows], [2])

    def test_reingesting_same_run_does_not_inflate_seen_count(self):
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        r = run(bugs=[bug])
        ingest.ingest(self.con, r)
        ingest.ingest(self.con, r)
        self.assertEqual(self.con.execute("SELECT seen_count FROM bugs").fetchone()[0], 1)

    def test_rewritten_run_replaces_its_bugs_and_counts_runs_once(self):
        """A run.json rewritten mid-run (or ingested by a sibling explorer's hook first) replaces its earlier version."""
        bugs = lambda: [tuple(r) for r in self.con.execute("SELECT title, seen_count FROM bugs ORDER BY title")]
        x, y = {"route": "/f", "title": "X", "symptom": "x"}, {"route": "/f", "title": "Y", "symptom": "y"}
        a = lambda *b: {"run_id": "explore-f-1", "build": "1", "bugs": list(b)}
        ingest.ingest(self.con, a({**x, "symptom": "first guess"}))
        ingest.ingest(self.con, a(x))  # symptom corrected: the first guess was never a separate bug
        self.assertEqual(bugs(), [("X", 1)])
        ingest.ingest(self.con, {"run_id": "explore-g-1", "build": "1", "bugs": [x]})
        ingest.ingest(self.con, a(x, y))  # run A rewritten after run B saw X: still two runs
        self.assertEqual(bugs(), [("X", 2), ("Y", 1)])
        ingest.ingest(self.con, a())  # A drops both: Y was only A's, X stays B's sighting
        self.assertEqual(bugs(), [("X", 1)])
        ingest.ingest(self.con, a(x))  # and back again
        self.assertEqual(bugs(), [("X", 2)])
        self.con.execute("UPDATE bugs SET status='FILED'")
        ingest.ingest(self.con, {"run_id": "explore-h-1", "build": "1", "bugs": [y]})
        ingest.ingest(self.con, {"run_id": "explore-h-1", "build": "1", "bugs": []})
        self.assertEqual(bugs(), [("X", 2)])
        # two files that reuse one run id are separate files, not rewrites of each other
        ingest.ingest(self.con, {"run_id": "same", "build": "1", "bugs": [y]}, src="qa-out/r/.data/run-a.json")
        ingest.ingest(self.con, {"run_id": "same", "build": "1", "bugs": [x]}, src="qa-out/r/.data/run-b.json")
        self.assertEqual(bugs(), [("X", 3), ("Y", 1)])

    def test_a_re_run_under_a_new_run_id_keeps_the_bug_the_earlier_attempt_found(self):
        """dispatch gives each attempt its own run_id: a re-run's file that doesn't list the bug is another run's file,
        not a rewrite of the earlier one (reusing the id deleted a bug on a real machine)."""
        src = self.proj / "qa-out/2026-11-25-PROJ-1/.data/run-form.json"
        x = {"route": "/form", "title": "Saves 131", "symptom": "saves 131"}
        ingest.ingest(self.con, {"run_id": "ticket-form-20261125-1000", "build": "1", "bugs": [x]}, src=src)
        ingest.ingest(self.con, {"run_id": "ticket-form-20261125-1100", "build": "1", "bugs": []}, src=src)
        self.con.commit()
        code, out, _ = qa_mem("bugs")
        self.assertEqual(code, 0)
        self.assertIn("Saves 131", out)

    def test_a_case_may_carry_the_build_and_date_it_ran_on(self):  # carry_prior writes them on a kept result
        case = lambda **kw: {"run_id": "r", "cases": [{"id": "TC-1", "status": "PASS", **kw}]}
        ingest.check_run(case(build="1.42", ran="2026-11-20"))
        for bad in ({"build": 1.42}, {"ran": ["2026-11-20"]}):
            with self.assertRaises(ValueError, msg=bad):
                ingest.check_run(case(**bad))

    def test_a_bug_dropped_because_no_run_lists_it_leaves_a_notice(self):
        """Memory deletes it on its own (a checkpoint listed it, the final run file didn't): the plugin log says so."""
        src = self.proj / "qa-out/2026-11-25-PROJ-1/.data/run-form.json"
        src.parent.mkdir(parents=True)
        log = self.proj / "qa-out/plugin-log.jsonl"
        notices = lambda: [r for r in map(json.loads, log.read_text().splitlines()) if r["type"] == "notice"]
        x, y = {"route": "/form/7", "title": "X", "symptom": "x"}, {"route": "/form", "title": "Y", "symptom": "y"}
        a = lambda *b: {"run_id": "explore-f-1", "build": "1", "bugs": list(b)}
        ingest.ingest(self.con, a(x, y), src=src)
        ingest.ingest(self.con, {"run_id": "explore-g-1", "build": "1", "bugs": [y]}, src="other")
        ingest.ingest(self.con, a(x, y), src=src)
        self.assertFalse(log.exists())
        ingest.ingest(self.con, a(), src=src)  # X was only this run's: dropped; Y another run still lists
        (n,) = notices()
        self.assertEqual(
            (n["kind"], n["hash"], n["route"], n["run"]),
            ("bug_dropped", core.bug_hash("/form/:id", "x"), "/form/:id", "explore-f-1"),
        )
        ingest.ingest(self.con, {"run_id": "r2", "build": "1", "bugs": [x]}, src="nowhere/run-x.json")
        ingest.ingest(self.con, {"run_id": "r2", "build": "1", "bugs": []}, src="nowhere/run-x.json")
        self.assertEqual(len(notices()), 1)  # no qa-out folder to tell: dropped all the same
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 1)

    def test_late_run_keeps_newer_coverage_and_bug_dates(self):
        cov = lambda r: [{"target": "speed", "heuristic": "boundary", "result": r}]
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        ingest.ingest(self.con, run(coverage=cov("fail"), bugs=[bug]), ts="2026-02-01T00:00:00Z")
        ingest.ingest(
            self.con, run(coverage=cov("pass"), bugs=[bug]), ts="2026-01-01T00:00:00Z"
        )  # an older file, ingested late
        self.assertEqual(
            self.con.execute("SELECT result, ts FROM coverage").fetchall()[0][:], ("fail", "2026-02-01T00:00:00Z")
        )
        self.assertEqual(
            tuple(self.con.execute("SELECT first_seen, last_seen, seen_count FROM bugs").fetchone()),
            ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 2),
        )
        ingest.ingest(self.con, run(coverage=cov("pass")), ts="2026-03-01T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT result FROM coverage").fetchone()[0], "pass")

    def test_a_sighting_older_than_the_last_one_predates_the_fix(self):
        # a sighting older than the last one (ingested late) predates the fix
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        ingest.ingest(self.con, run(bugs=[bug]), ts="2026-11-10T00:00:00Z")
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        self.con.execute("UPDATE bugs SET status='FIXED'")
        newest = self.con.execute("SELECT last_run FROM bugs").fetchone()[0]
        ingest.ingest(self.con, run(bugs=[bug]), ts="2026-11-05T00:00:00Z")
        self.assertEqual(tuple(self.con.execute("SELECT status, last_run FROM bugs").fetchone()), ("FIXED", newest))
        ingest.ingest(self.con, run(bugs=[bug]), ts="2026-11-20T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT status FROM bugs WHERE hash=?", (h,)).fetchone()[0], "REGRESSED")

    def test_a_run_from_before_the_fix_ingested_late_does_not_regress(self):
        """A run that finished after the last sighting but before the fix, ingested after the fix, predates it."""
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        ingest.ingest(self.con, run(bugs=[bug]), ts="2026-11-10T00:00:00Z")
        h = core.bug_hash("/form", "s")
        with mock.patch.object(core, "now", return_value="2026-11-15T00:00:00Z"):
            cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        status = lambda: self.con.execute("SELECT status FROM bugs WHERE hash=?", (h,)).fetchone()[0]
        ingest.ingest(self.con, run(bugs=[bug]), ts="2026-11-12T00:00:00Z")
        self.assertEqual(status(), "FIXED")
        ingest.ingest(self.con, run(bugs=[bug]), ts="2026-11-16T00:00:00Z")
        self.assertEqual(status(), "REGRESSED")

    def test_reopening_a_bug_no_run_lists_removes_it(self):
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        r = run(bugs=[bug])
        ingest.ingest(self.con, r, src="a")
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        cli.run_cmd(self.con, "bug-status", [h, "FILED"])
        ingest.ingest(self.con, {**r, "bugs": []}, src="a")  # the run file was rewritten without it; triaged, so kept
        self.assertEqual(self.con.execute("SELECT status, seen_count FROM bugs").fetchone()[:], ("FILED", 0))
        cli.run_cmd(self.con, "bug-status", [h, "OPEN"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 0)

    def test_fixed_bug_seen_again_regresses_and_symptom_shown(self):
        bug = {"route": "/form", "title": "t", "symptom": "speed 999 saved"}
        ingest.ingest(self.con, run(bugs=[bug]))
        h = core.bug_hash("/form", "speed 999 saved")
        self.assertEqual(cli.main(["bug-status", h[:6], "fixed"]), 0)
        self.assertIn(
            f"PREVIOUSLY FIXED (seen again = regression: put its hash in run.json):\n- {h} FIXED",
            cards.card(self.con, "/form"),
        )
        ingest.ingest(self.con, run("1.1", bugs=[{"route": "/form", "title": "other words", "hash": h}]))
        c = cards.card(self.con, "/form")
        self.assertIn(f"{h} REGRESSED", c)
        self.assertIn("symptom: speed 999 saved", c)

    def test_empty_or_numeric_draft_path(self):
        bug = {"route": "/form", "title": "t", "symptom": "s", "draft_path": "/abs/bugs/BUG-1.md"}
        ingest.ingest(self.con, run(bugs=[bug]))
        ingest.ingest(
            self.con, run(bugs=[{**bug, "draft_path": ""}]), run_dir=Path(self.tmp.name)
        )  # a re-sighting without one
        self.assertEqual(self.con.execute("SELECT draft_path FROM bugs").fetchone()[0], "/abs/bugs/BUG-1.md")
        ingest.ingest(self.con, run(bugs=[{**bug, "symptom": "other", "draft_path": 7}]), run_dir=Path(self.tmp.name))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 2)

    def test_a_hash_prefix_finds_the_bug(self):
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]))
        h = core.bug_hash("/form", "s")
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "x", "hash": h[:8]}]))
        self.assertEqual(self.con.execute("SELECT count(*), max(seen_count) FROM bugs").fetchone()[:], (1, 2))

    def test_a_draft_id_in_hash_is_still_stored(self):
        ingest.ingest(
            self.con,
            run(bugs=[{"route": "/form", "title": "Save accepts 131", "symptom": "saved at 131", "hash": "BUG-2"}]),
        )
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 1)

    def test_jira_key_in_hash_creates_no_local_bug(self):
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "Known jira PROJ-9 bug", "hash": "PROJ-9"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 0)

    def test_hash_field_variants(self):
        for i, junk in enumerate(["n/a", "none", "new", "TBD", ""]):
            ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": f"t{i}", "symptom": f"s{i}", "hash": junk}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 5)
        h = core.bug_hash("/form", "s0")
        self.con.execute("UPDATE bugs SET status='FIXED' WHERE hash=?", (h,))
        self.con.commit()
        for variant in [f"{h} ", f"#{h}", f"{h} (known)"]:
            ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "x", "hash": variant}]))
        self.assertEqual(
            self.con.execute("SELECT status, seen_count FROM bugs WHERE hash=?", (h,)).fetchone()[:], ("REGRESSED", 4)
        )
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "jira dup", "hash": "PROJ-9"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 5)

    def test_invalid_bugs_listed_as_not_bugs(self):
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "plural wording"}]))
        h = core.bug_hash("/form", "plural wording")
        self.con.close()
        cli.main(["bug-status", h, "INVALID"])
        self.con = core.connect()
        self.assertIn(
            f"NOT BUGS (the user marked these INVALID: don't report them):\n- {h} INVALID",
            cards.card(self.con, "/form"),
        )

    def test_filed_bugs_stay_visible(self):
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "t"}]))
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        cli.main(["bug-status", h, "FILED"])
        self.assertIn("FILED", cards.card(self.con, "/form"))


class BugLines(MemBase):
    def test_bugs_lines_name_the_status(self):
        """prep joins OPEN, FILED and REGRESSED lists into one: each line must say which it is."""
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]))
        self.con.execute("UPDATE bugs SET status='REGRESSED'")
        self.assertIn(" REGRESSED ", memory.bugs(self.con, "REGRESSED"))

    def test_a_bug_without_severity_or_classification_prints_no_none(self):
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "Bare bug"}]))
        self.assertNotIn("None", memory.bugs(self.con))
        self.assertNotIn("None", cards.card(self.con, "/form"))


class BugStatus(MemBase):
    """`qa-mem bug-status` and `qa-mem hash`."""

    def test_bug_status_requires_prefix_and_valid_status(self):
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "t"}]))
        self.assertEqual(cli.main(["bug-status", "", "FIXED"]), 2)
        self.assertEqual(cli.main(["bug-status", "abcd", "WHATEVER"]), 2)
        self.assertEqual(self.con.execute("SELECT status FROM bugs").fetchone()[0], "OPEN")

    def test_bug_status_refuses_ambiguous_prefix(self):
        with self.con:
            for h in ("abcd11111111", "abcd22222222"):
                self.con.execute("INSERT INTO bugs(hash,page,title) VALUES(?,?,?)", (h, "/form", "t"))
        self.assertEqual(cli.main(["bug-status", "abcd", "FIXED"]), 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs WHERE status='FIXED'").fetchone()[0], 0)
        self.assertEqual(cli.main(["bug-status", "abcd1", "FIXED"]), 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs WHERE status='FIXED'").fetchone()[0], 1)

    def test_bug_status_wildcards_and_stale_extra_args(self):
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "a"}, {"route": "/form", "title": "b"}]))
        self.assertEqual(cli.main(["bug-status", "____", "INVALID"]), 2)
        self.assertEqual(cli.main(["stale", "2.0", "x"]), 0)

    def test_hash_cli_matches_ingest_normalisation(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["hash", "http://localhost:8765/form/", "speed", "999"])
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "speed 999"}]))
        self.assertEqual(buf.getvalue().strip(), self.con.execute("SELECT hash FROM bugs").fetchone()[0])

    def test_hash_cli_redacts_like_ingest(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["hash", "/form", "500 with cookie: sid=abc123"])
        ingest.ingest(
            self.con,
            scrub.redact_json(run(bugs=[{"route": "/form", "title": "t", "symptom": "500 with cookie: sid=abc123"}])),
        )
        self.assertEqual(buf.getvalue().strip(), self.con.execute("SELECT hash FROM bugs").fetchone()[0])

    def test_bug_status_with_jira_key_and_v04_schema_upgrade(self):
        # Existing v0.4 DB (has last_run + symptom, lacks jira_key) upgrades in place
        v04 = Path(self.tmp.name) / "v04.db"
        c = sqlite3.connect(v04)
        c.execute(
            "CREATE TABLE bugs(hash TEXT PRIMARY KEY, page TEXT, title TEXT, severity TEXT, classification TEXT, "
            "status TEXT DEFAULT 'OPEN', draft_path TEXT, first_seen TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 1, "
            "last_run TEXT, symptom TEXT)"
        )
        c.execute(
            "INSERT INTO bugs(hash,page,title,status,last_run,symptom) VALUES('abcd12345678','/form','old','OPEN','r1','s')"
        )
        c.commit()
        c.close()
        os.environ["QA_MEM_DB"] = str(v04)
        upgraded = core.connect()
        try:
            cols = {r[1] for r in upgraded.execute("PRAGMA table_info(bugs)")}
            self.assertIn("jira_key", cols)
            self.assertEqual(upgraded.execute("SELECT title FROM bugs WHERE hash='abcd12345678'").fetchone()[0], "old")
        finally:
            upgraded.close()
        # bug-status stores jira_key (positional or --jira) and displays it on bugs & card
        code, out, _ = qa_mem("bug-status", "abcd1234", "FILED", "proj-99")
        self.assertEqual(code, 0)
        self.assertIn("PROJ-99", out)
        self.assertIn("PROJ-99", qa_mem("bugs", "FILED")[1])
        self.assertIn("PROJ-99", qa_mem("card", "/form")[1])
        code, out, _ = qa_mem("bug-status", "abcd1234", "FILED", "--jira", "OPS-7")
        self.assertEqual((code, "OPS-7" in out), (0, True))
        self.assertEqual(qa_mem("bug-status", "abcd1234", "FILED", "not-a-key")[0], 2)


class BugSync(MemBase):
    """`qa-mem bug-sync`: read-only Jira status sync for FILED bugs."""

    def add_bug(self, route, symptom, status="FILED", jira_key=None):
        ingest.ingest(self.con, run(bugs=[{"route": route, "title": symptom, "symptom": symptom}]))
        h = core.bug_hash(route, symptom)
        with self.con:
            self.con.execute("UPDATE bugs SET status=?, jira_key=? WHERE hash=?", (status, jira_key, h))
        return h

    def test_jql_and_transitions(self):
        self.assertEqual(qa_mem("bug-sync", "--jql"), (0, "", ""))
        h_done = self.add_bug("/form", "speed 131 saved", "FILED", "PROJ-10")
        h_wont = self.add_bug("/list", "header align", "FILED", "PROJ-11")
        h_open = self.add_bug("/map", "zoom drift", "FILED", "PROJ-12")
        h_local = self.add_bug("/form", "local open", "OPEN", "PROJ-13")
        code, jql, err = qa_mem("bug-sync", "--jql")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(jql.strip(), "key in (PROJ-10, PROJ-11, PROJ-12)")
        payload = json.dumps(
            [
                {
                    "key": "PROJ-10",
                    "fields": {
                        "status": {"name": "Done", "statusCategory": {"key": "done"}},
                        "resolution": {"name": "Fixed"},
                    },
                },
                {
                    "key": "PROJ-11",
                    "fields": {
                        "status": {"name": "Closed", "statusCategory": {"key": "done"}},
                        "resolution": {"name": "Won't Do"},
                    },
                },
                {
                    "key": "PROJ-12",
                    "fields": {"status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
                },
                {"key": "PROJ-13", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}},
            ]
        )
        code, out, err = qa_mem("bug-sync", stdin=payload)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("PROJ-10", out)
        self.assertIn("/form", out)
        self.assertIn(f"qa-mem bug-status {h_wont} INVALID", out)
        statuses = dict(self.con.execute("SELECT hash, status FROM bugs").fetchall())
        self.assertEqual(statuses[h_done], "FIXED")
        self.assertEqual(statuses[h_wont], "FILED")
        self.assertEqual(statuses[h_open], "FILED")
        self.assertEqual(statuses[h_local], "OPEN")
        self.assertEqual(qa_mem("bug-sync", stdin="{bad")[0], 2)

    def test_bug_sync_fetches_resolution_via_view_when_search_omits_it(self):
        import subprocess

        from support import done

        h_dup = self.add_bug("/form", "dup bug", "FILED", "PROJ-20")
        search_out = json.dumps(
            [{"key": "PROJ-20", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}}]
        )
        view_out = json.dumps({"key": "PROJ-20", "fields": {"resolution": {"name": "Duplicate"}}})
        with (
            mock.patch("shutil.which", return_value="/usr/bin/acli"),
            mock.patch.object(subprocess, "run", return_value=done(0, out=view_out)) as mrun,
        ):
            code, out, err = qa_mem("bug-sync", stdin=search_out)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("closed as Duplicate", out)
        self.assertEqual(self.con.execute("SELECT status FROM bugs WHERE hash=?", (h_dup,)).fetchone()[0], "FILED")
        self.assertEqual(
            mrun.call_args[0][0],
            ["/usr/bin/acli", "jira", "workitem", "view", "PROJ-20", "--fields", "resolution", "--json"],
        )

    def test_a_resolution_that_cannot_be_read_leaves_the_bug_filed(self):
        """A failed lookup is no 'Fixed' resolution: a Won't Do / Duplicate must not become FIXED because acli failed."""
        import subprocess

        from support import done

        h = self.add_bug("/form", "dup bug", "FILED", "PROJ-20")
        h_ok = self.add_bug("/list", "fixed bug", "FILED", "PROJ-21")
        search_out = json.dumps(
            [
                {"key": "PROJ-20", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}},
                {
                    "key": "PROJ-21",
                    "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}, "resolution": None},
                },
            ]
        )
        with (
            mock.patch("shutil.which", return_value="/usr/bin/acli"),
            mock.patch.object(subprocess, "run", return_value=done(1, err="401")),
        ):
            code, out, err = qa_mem("bug-sync", stdin=search_out)
        self.assertEqual((code, err), (0, ""))
        statuses = dict(self.con.execute("SELECT hash, status FROM bugs").fetchall())
        self.assertEqual(statuses[h], "FILED")
        self.assertIn("PROJ-20", out.split("couldn't read", 1)[1])
        self.assertEqual(statuses[h_ok], "FIXED")  # Done with an empty resolution, read fine: fixed, as before
        self.assertIsNotNone(self.con.execute("SELECT fixed_ts FROM bugs WHERE hash=?", (h_ok,)).fetchone()[0])


class BugMatch(MemBase):
    """`qa-mem bug-match <route> <symptom> [--endpoint]`: a likely duplicate before a new draft is written."""

    def setUp(self):
        super().setUp()
        with self.con:
            for page in ("/zones/new", "/zones/:id"):
                self.con.execute(
                    "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                    (page, "api", "save", "POST /api/zones", "a.js:1", "/repo", "frontend", core.now(), "a.js"),
                )

    def bug(self, page, title, symptom, status="OPEN", **extra):
        ingest.ingest(
            self.con,
            {
                "run_id": f"r-{symptom}",
                "build": "1",
                "pages": [{"route": page}],
                "bugs": [{"route": page, "title": title, "symptom": symptom, "severity": "HIGH", **extra}],
            },
        )
        h = core.bug_hash(page, symptom)
        self.con.execute("UPDATE bugs SET status=? WHERE hash=?", (status, h))
        return h

    def test_a_reworded_bug_seen_from_another_page_is_found(self):
        h = self.bug("/zones/new", "Speed 131 saved", "speed 131 saved without error", endpoint="POST /api/zones")
        self.bug("/zones/new", "Header misaligned", "header misaligned on narrow screens")
        out = memory.bug_match(self.con, "/zones/:id", "form saves speed limit above 130 (131)", "POST /api/zones")
        self.assertIn(h, out)
        self.assertIn("report a sighting with its hash", out)
        self.assertNotIn("Header misaligned", out)
        code, cli_out, _ = qa_mem(
            "bug-match", "/zones/:id", "form saves speed limit above 130 (131)", "--endpoint", "POST /api/zones"
        )
        self.assertEqual(code, 0)
        self.assertIn(h, cli_out)

    def test_fixed_and_invalid_matches_say_what_to_do(self):
        fixed = self.bug("/zones/new", "Speed 131 saved", "speed 131 saved", "FIXED")
        invalid = self.bug("/zones/new", "Speed label typo", "speed field label typo", "INVALID")
        self.assertIn("regression", memory.bug_match(self.con, "/zones/new", "speed 131 is saved again"))
        out = memory.bug_match(self.con, "/zones/new", "typo in the speed field label")
        self.assertIn(invalid, out)
        self.assertIn("NOT A BUG", out)
        self.assertNotIn(fixed, out)

    def test_a_bug_on_an_unrelated_page_is_no_match(self):
        self.bug("/incidents", "Speed 131 saved", "speed 131 saved")  # same words, a page sharing nothing
        self.assertIn("no likely duplicate", memory.bug_match(self.con, "/zones/new", "speed 131 saved"))


if __name__ == "__main__":
    unittest.main()
