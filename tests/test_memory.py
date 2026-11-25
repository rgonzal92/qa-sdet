"""Memory (qa_sdet.memory, core): ingest of run files, bug records, build staleness, the page card and search, the
bug-status and hash commands, and the DB itself."""
import contextlib, io, json, os, sqlite3, time, unittest
from pathlib import Path
from unittest import mock

from support import MemBase, runfile, run, PLAN, RUN_FORM, RUN_LIST, core, memory, cli as qa_mem, git, git_repo
from qa_sdet import cli, scrub


class Bugs(MemBase):
    """How a bug in a run becomes one record: dedup, hash, sightings, status."""

    def test_bug_dedup_increments_seen_count(self):
        bug = {"route": "/form", "title": "Speed accepts 999", "symptom": "speed accepts 999",
               "severity": "HIGH", "classification": "CONFIRMED"}
        memory.ingest(self.con, run(bugs=[bug]))
        memory.ingest(self.con, run(bugs=[{**bug, "symptom": "Speed  accepts 999!"}]))
        rows = self.con.execute("SELECT seen_count FROM bugs").fetchall()
        self.assertEqual([r[0] for r in rows], [2])

    def test_reingesting_same_run_does_not_inflate_seen_count(self):
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        r = run(bugs=[bug])
        memory.ingest(self.con, r)
        memory.ingest(self.con, r)
        self.assertEqual(self.con.execute("SELECT seen_count FROM bugs").fetchone()[0], 1)

    def test_rewritten_run_replaces_its_bugs_and_counts_runs_once(self):
        """A run.json rewritten mid-run (or ingested by a sibling explorer's hook first) replaces its earlier version."""
        bugs = lambda: [tuple(r) for r in self.con.execute("SELECT title, seen_count FROM bugs ORDER BY title")]
        x, y = {"route": "/f", "title": "X", "symptom": "x"}, {"route": "/f", "title": "Y", "symptom": "y"}
        a = lambda *b: {"run_id": "explore-f-1", "build": "1", "bugs": list(b)}
        memory.ingest(self.con, a({**x, "symptom": "first guess"}))
        memory.ingest(self.con, a(x))  # symptom corrected: the first guess was never a separate bug
        self.assertEqual(bugs(), [("X", 1)])
        memory.ingest(self.con, {"run_id": "explore-g-1", "build": "1", "bugs": [x]})
        memory.ingest(self.con, a(x, y))  # run A rewritten after run B saw X: still two runs
        self.assertEqual(bugs(), [("X", 2), ("Y", 1)])
        memory.ingest(self.con, a())  # A drops both: Y was only A's, X stays B's sighting
        self.assertEqual(bugs(), [("X", 1)])
        memory.ingest(self.con, a(x))  # and back again
        self.assertEqual(bugs(), [("X", 2)])
        self.con.execute("UPDATE bugs SET status='FILED'")
        memory.ingest(self.con, {"run_id": "explore-h-1", "build": "1", "bugs": [y]})
        memory.ingest(self.con, {"run_id": "explore-h-1", "build": "1", "bugs": []})
        self.assertEqual(bugs(), [("X", 2)])
        # two files that reuse one run id are separate files, not rewrites of each other
        memory.ingest(self.con, {"run_id": "same", "build": "1", "bugs": [y]}, src="qa-out/r/.data/run-a.json")
        memory.ingest(self.con, {"run_id": "same", "build": "1", "bugs": [x]}, src="qa-out/r/.data/run-b.json")
        self.assertEqual(bugs(), [("X", 3), ("Y", 1)])

    def test_late_run_keeps_newer_coverage_and_bug_dates(self):
        cov = lambda r: [{"target": "speed", "heuristic": "boundary", "result": r}]
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        memory.ingest(self.con, run(coverage=cov("fail"), bugs=[bug]), ts="2026-02-01T00:00:00Z")
        memory.ingest(self.con, run(coverage=cov("pass"), bugs=[bug]), ts="2026-01-01T00:00:00Z")  # an older file, ingested late
        self.assertEqual(self.con.execute("SELECT result, ts FROM coverage").fetchall()[0][:], ("fail", "2026-02-01T00:00:00Z"))
        self.assertEqual(tuple(self.con.execute("SELECT first_seen, last_seen, seen_count FROM bugs").fetchone()),
                         ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 2))
        memory.ingest(self.con, run(coverage=cov("pass")), ts="2026-03-01T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT result FROM coverage").fetchone()[0], "pass")

    def test_a_sighting_older_than_the_last_one_predates_the_fix(self):
        # a sighting older than the last one (ingested late) predates the fix
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-10T00:00:00Z")
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        self.con.execute("UPDATE bugs SET status='FIXED'")
        newest = self.con.execute("SELECT last_run FROM bugs").fetchone()[0]
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-05T00:00:00Z")
        self.assertEqual(tuple(self.con.execute("SELECT status, last_run FROM bugs").fetchone()), ("FIXED", newest))
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-20T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT status FROM bugs WHERE hash=?", (h,)).fetchone()[0], "REGRESSED")

    def test_a_run_from_before_the_fix_ingested_late_does_not_regress(self):
        """A run that finished after the last sighting but before the fix, ingested after the fix, predates it."""
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-10T00:00:00Z")
        h = core.bug_hash("/form", "s")
        with mock.patch.object(core, "now", return_value="2026-11-15T00:00:00Z"):
            cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        status = lambda: self.con.execute("SELECT status FROM bugs WHERE hash=?", (h,)).fetchone()[0]
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-12T00:00:00Z")
        self.assertEqual(status(), "FIXED")
        memory.ingest(self.con, run(bugs=[bug]), ts="2026-11-16T00:00:00Z")
        self.assertEqual(status(), "REGRESSED")

    def test_reopening_a_bug_no_run_lists_removes_it(self):
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        r = run(bugs=[bug])
        memory.ingest(self.con, r, src="a")
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        cli.run_cmd(self.con, "bug-status", [h, "FILED"])
        memory.ingest(self.con, {**r, "bugs": []}, src="a")  # the run file was rewritten without it; triaged, so kept
        self.assertEqual(self.con.execute("SELECT status, seen_count FROM bugs").fetchone()[:], ("FILED", 0))
        cli.run_cmd(self.con, "bug-status", [h, "OPEN"])
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 0)

    def test_fixed_bug_seen_again_regresses_and_symptom_shown(self):
        bug = {"route": "/form", "title": "t", "symptom": "speed 999 saved"}
        memory.ingest(self.con, run(bugs=[bug]))
        h = core.bug_hash("/form", "speed 999 saved")
        self.assertEqual(cli.main(["bug-status", h[:6], "fixed"]), 0)
        self.assertIn(f"PREVIOUSLY FIXED (seen again = regression: put its hash in run.json):\n- {h} FIXED",
                      memory.card(self.con, "/form"))
        memory.ingest(self.con, run("1.1", bugs=[{"route": "/form", "title": "other words", "hash": h}]))
        c = memory.card(self.con, "/form")
        self.assertIn(f"{h} REGRESSED", c)
        self.assertIn("symptom: speed 999 saved", c)

    def test_empty_or_numeric_draft_path(self):
        bug = {"route": "/form", "title": "t", "symptom": "s", "draft_path": "/abs/bugs/BUG-1.md"}
        memory.ingest(self.con, run(bugs=[bug]))
        memory.ingest(self.con, run(bugs=[{**bug, "draft_path": ""}]), run_dir=Path(self.tmp.name))  # a re-sighting without one
        self.assertEqual(self.con.execute("SELECT draft_path FROM bugs").fetchone()[0], "/abs/bugs/BUG-1.md")
        memory.ingest(self.con, run(bugs=[{**bug, "symptom": "other", "draft_path": 7}]), run_dir=Path(self.tmp.name))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 2)

    def test_a_hash_prefix_finds_the_bug(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]))
        h = core.bug_hash("/form", "s")
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "x", "hash": h[:8]}]))
        self.assertEqual(self.con.execute("SELECT count(*), max(seen_count) FROM bugs").fetchone()[:], (1, 2))

    def test_a_draft_id_in_hash_is_still_stored(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "Save accepts 131", "symptom": "saved at 131",
                                       "hash": "BUG-2"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 1)

    def test_jira_key_in_hash_creates_no_local_bug(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "Known jira PROJ-9 bug", "hash": "PROJ-9"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 0)

    def test_hash_field_variants(self):
        for i, junk in enumerate(["n/a", "none", "new", "TBD", ""]):
            memory.ingest(self.con, run(bugs=[{"route": "/form", "title": f"t{i}", "symptom": f"s{i}", "hash": junk}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 5)
        h = core.bug_hash("/form", "s0")
        self.con.execute("UPDATE bugs SET status='FIXED' WHERE hash=?", (h,))
        self.con.commit()
        for variant in [f"{h} ", f"#{h}", f"{h} (known)"]:
            memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "x", "hash": variant}]))
        self.assertEqual(self.con.execute("SELECT status, seen_count FROM bugs WHERE hash=?", (h,)).fetchone()[:], ("REGRESSED", 4))
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "jira dup", "hash": "PROJ-9"}]))
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 5)

    def test_invalid_bugs_listed_as_not_bugs(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "plural wording"}]))
        h = core.bug_hash("/form", "plural wording")
        self.con.close()
        cli.main(["bug-status", h, "INVALID"])
        self.con = core.connect()
        self.assertIn(f"NOT BUGS (the user marked these INVALID: don't report them):\n- {h} INVALID", memory.card(self.con, "/form"))

    def test_filed_bugs_stay_visible(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t"}]))
        h = self.con.execute("SELECT hash FROM bugs").fetchone()[0]
        cli.main(["bug-status", h, "FILED"])
        self.assertIn("FILED", memory.card(self.con, "/form"))


class BugLines(MemBase):
    def test_bugs_lines_name_the_status(self):
        """prep joins OPEN, FILED and REGRESSED lists into one: each line must say which it is."""
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]))
        self.con.execute("UPDATE bugs SET status='REGRESSED'")
        self.assertIn(" REGRESSED ", memory.bugs(self.con, "REGRESSED"))

    def test_a_bug_without_severity_or_classification_prints_no_none(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "Bare bug"}]))
        self.assertNotIn("None", memory.bugs(self.con))
        self.assertNotIn("None", memory.card(self.con, "/form"))


class BugStatus(MemBase):
    """`qa-mem bug-status` and `qa-mem hash`."""

    def test_bug_status_requires_prefix_and_valid_status(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t"}]))
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
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "a"}, {"route": "/form", "title": "b"}]))
        self.assertEqual(cli.main(["bug-status", "____", "INVALID"]), 2)
        self.assertEqual(cli.main(["stale", "2.0", "x"]), 0)

    def test_hash_cli_matches_ingest_normalisation(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["hash", "http://localhost:8765/form/", "speed", "999"])
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "speed 999"}]))
        self.assertEqual(buf.getvalue().strip(), self.con.execute("SELECT hash FROM bugs").fetchone()[0])

    def test_hash_cli_redacts_like_ingest(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["hash", "/form", "500 with cookie: sid=abc123"])
        memory.ingest(self.con, scrub.redact_json(run(bugs=[{"route": "/form", "title": "t", "symptom": "500 with cookie: sid=abc123"}])))
        self.assertEqual(buf.getvalue().strip(), self.con.execute("SELECT hash FROM bugs").fetchone()[0])

    def test_bug_status_with_jira_key_and_v04_schema_upgrade(self):
        # Existing v0.4 DB (has last_run + symptom, lacks jira_key) upgrades in place
        v04 = Path(self.tmp.name) / "v04.db"
        c = sqlite3.connect(v04)
        c.execute("CREATE TABLE bugs(hash TEXT PRIMARY KEY, page TEXT, title TEXT, severity TEXT, classification TEXT, "
                  "status TEXT DEFAULT 'OPEN', draft_path TEXT, first_seen TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 1, "
                  "last_run TEXT, symptom TEXT)")
        c.execute("INSERT INTO bugs(hash,page,title,status,last_run,symptom) VALUES('abcd12345678','/form','old','OPEN','r1','s')")
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
        memory.ingest(self.con, run(bugs=[{"route": route, "title": symptom, "symptom": symptom}]))
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
        payload = json.dumps([
            {"key": "PROJ-10", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}},
                                          "resolution": {"name": "Fixed"}}},
            {"key": "PROJ-11", "fields": {"status": {"name": "Closed", "statusCategory": {"key": "done"}},
                                          "resolution": {"name": "Won't Do"}}},
            {"key": "PROJ-12", "fields": {"status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}}},
            {"key": "PROJ-13", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}},
        ])
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
        search_out = json.dumps([{"key": "PROJ-20", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}}])
        view_out = json.dumps({"key": "PROJ-20", "fields": {"resolution": {"name": "Duplicate"}}})
        with mock.patch("shutil.which", return_value="/usr/bin/acli"), \
                mock.patch.object(subprocess, "run", return_value=done(0, out=view_out)) as mrun:
            code, out, err = qa_mem("bug-sync", stdin=search_out)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("closed as Duplicate", out)
        self.assertEqual(self.con.execute("SELECT status FROM bugs WHERE hash=?", (h_dup,)).fetchone()[0], "FILED")
        self.assertEqual(mrun.call_args[0][0], ["/usr/bin/acli", "jira", "workitem", "view", "PROJ-20", "--fields", "resolution", "--json"])

    def test_a_resolution_that_cannot_be_read_leaves_the_bug_filed(self):
        """A failed lookup is no 'Fixed' resolution: a Won't Do / Duplicate must not become FIXED because acli failed."""
        import subprocess
        from support import done
        h = self.add_bug("/form", "dup bug", "FILED", "PROJ-20")
        h_ok = self.add_bug("/list", "fixed bug", "FILED", "PROJ-21")
        search_out = json.dumps([
            {"key": "PROJ-20", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}},
            {"key": "PROJ-21", "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}, "resolution": None}}])
        with mock.patch("shutil.which", return_value="/usr/bin/acli"), \
                mock.patch.object(subprocess, "run", return_value=done(1, err="401")):
            code, out, err = qa_mem("bug-sync", stdin=search_out)
        self.assertEqual((code, err), (0, ""))
        statuses = dict(self.con.execute("SELECT hash, status FROM bugs").fetchall())
        self.assertEqual(statuses[h], "FILED")
        self.assertIn("PROJ-20", out.split("couldn't read", 1)[1])
        self.assertEqual(statuses[h_ok], "FIXED")  # Done with an empty resolution, read fine: fixed, as before
        self.assertIsNotNone(self.con.execute("SELECT fixed_ts FROM bugs WHERE hash=?", (h_ok,)).fetchone()[0])


class Builds(MemBase):
    """A new build marks old knowledge stale; a plan or an unreadable build never does."""

    def test_new_build_marks_old_knowledge_stale(self):
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "old rule"}],
                                locators=[{"name": "Save", "locator": "x"}]))
        memory.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "new rule"}]))
        c = memory.card(self.con, "/form")
        self.assertIn("old rule *", c)
        self.assertNotIn("new rule *", c)
        self.assertNotIn("LOCATORS", c)  # stale locator hidden
        # coverage from build 1.0 no longer counts
        self.assertIn("boundary", c.split("UNTRIED: ")[1])

    def test_stale_command_resets_coverage_view(self):
        memory.ingest(self.con, run("1.0", coverage=[{"target": "x", "heuristic": "boundary"}]))
        cli.main(["stale", "2.0"])
        c = memory.card(self.con, "/form")
        self.assertIn("re-verify", c)
        self.assertIn("boundary", c.split("UNTRIED: ")[1])

    def test_a_corrected_build_makes_knowledge_current_again(self):
        memory.ingest(self.con, run("1.43.0", facts=[{"kind": "rule", "text": "speed max 130"}]))
        cli.main(["stale", "1.44.0"])
        self.assertEqual(self.con.execute("SELECT max(stale) FROM facts").fetchone()[0], 1)
        cli.main(["stale", "1.43.0"])  # corrected: what was verified on 1.43.0 is current again
        self.assertEqual(self.con.execute("SELECT max(stale) FROM facts").fetchone()[0], 0)

    def test_unknown_build_plan_does_not_stale_real_knowledge(self):
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "real"}],
                                coverage=[{"target": "x", "heuristic": "boundary"}]))
        memory.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "from ticket"}]))
        c = memory.card(self.con, "/form")
        self.assertIn("build=1.0", c)
        self.assertNotIn("real *", c)
        self.assertIn("boundary(", c)

    def test_unreadable_build_spellings(self):
        memory.ingest(self.con, run("1.42.0", facts=[{"kind": "rule", "text": "real"}]))
        for b in ["undefined", "unknown (footer .version not found)", "Unknown", "UNKNOWN", "null", "N/A", ""]:
            memory.ingest(self.con, run(b, coverage=[{"target": "t", "heuristic": "duplicate"}]))
        c = memory.card(self.con, "/form")
        self.assertIn("build=1.42.0", c)
        self.assertNotIn("real *", c)
        self.assertEqual({r[0] for r in self.con.execute("SELECT DISTINCT build FROM coverage")}, {"unknown"})
        self.assertIn("duplicate(", c)  # unknown-build coverage visible (within the week)

    def test_build_whitespace_is_not_a_new_deploy(self):
        memory.ingest(self.con, run("Build:\n   1.42", facts=[{"kind": "rule", "text": "real"}]))
        memory.ingest(self.con, run("Build: 1.42", facts=[{"kind": "rule", "text": "other"}]))
        self.assertNotIn("real *", memory.card(self.con, "/form"))
        memory.ingest(self.con, run("x" * 5000, bugs=[{"route": "/form", "title": "t", "severity": "H" * 500}]))
        self.assertLessEqual(len(core.meta(self.con, "build")), core.MAX_TEXT)
        self.assertLessEqual(len(self.con.execute("SELECT severity FROM bugs").fetchone()[0]), 20)

    def test_a_late_run_of_an_older_build_does_not_roll_back_the_page_or_the_build(self):
        memory.ingest(self.con, run("1.1", facts=[{"kind": "rule", "text": "fresh"}]), "2026-11-22T10:00:00Z")
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "old"}]), "2026-11-21T10:00:00Z")
        c = memory.card(self.con, "/form")
        self.assertIn("build=1.1", c)
        self.assertEqual(core.meta(self.con, "build"), "1.1")
        self.assertIn("fresh\n", c + "\n")
        self.assertNotIn("fresh *", c)

    def test_late_run_of_the_current_build_still_adds_what_it_saw(self):
        memory.ingest(self.con, run("2.0", locators=[{"name": "Add", "locator": "getByRole('button', { name: 'Add' })"}]),
                  "2026-11-22T10:00:00Z")
        memory.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "code max 6"}],
                                locators=[{"name": "Code", "locator": "getByLabel('Code')"}]), "2026-11-22T09:00:00Z")
        names = {r[0] for r in self.con.execute("SELECT name FROM locators")}
        self.assertEqual(names, {"Add", "Code"})
        self.assertEqual(self.con.execute("SELECT stale FROM facts WHERE text='code max 6'").fetchone()[0], 0)
        memory.ingest(self.con, run("1.0", locators=[{"name": "Old", "locator": "#old"}]), "2026-11-21T09:00:00Z")
        self.assertNotIn("Old", {r[0] for r in self.con.execute("SELECT name FROM locators")})  # older build: skipped

    def test_a_plan_does_not_refresh_a_stale_fact(self):
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "r"}]), "2026-11-20T10:00:00Z")
        memory.ingest(self.con, run("1.1"), "2026-11-21T10:00:00Z")  # r now stale
        memory.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "r"}]), "2026-11-22T10:00:00Z")
        self.assertEqual(self.con.execute("SELECT stale, build FROM facts").fetchone()[:], (1, "1.0"))

    def test_coverage_of_an_unreadable_build_expires_after_a_week(self):
        memory.ingest(self.con, run("unknown", coverage=[{"target": "t", "heuristic": "duplicate"}]))
        self.assertIn("duplicate(", memory.card(self.con, "/form"))
        memory.ingest(self.con, run("unknown", coverage=[{"target": "t", "heuristic": "boundary"}]), "2020-01-01T00:00:00Z")
        self.assertNotIn("boundary(", memory.card(self.con, "/form"))

    def test_plan_does_not_mark_verified(self):
        memory.ingest(self.con, {"run_id": "p", "build": "unknown", "pages": [{"route": "/new"}]})
        self.assertIn("verified=never", memory.card(self.con, "/new"))

    def test_unknown_build_page_keeps_its_card(self):
        memory.ingest(self.con, {"run_id": "a", "build": "1.42.0", "pages": [{"route": "/incidents"}]})
        memory.ingest(self.con, {"run_id": "b", "build": "unknown", "pages": [{"route": "/map", "facts": [{"kind": "rule", "text": "layers switch"}],
                  "locators": [{"name": "Layer", "locator": "getByLabel('Layer')"}]}]})
        c = memory.card(self.con, "/map")
        self.assertIn("LOCATORS (fresh): Layer=getByLabel('Layer')", c)
        self.assertIn("- [rule] layers switch\n", c + "\n")  # not marked stale
        self.assertIn("never readable", c)


class Facts(MemBase):
    """What a run's facts, coverage and locators become."""

    def test_coverage_results_normalised(self):
        cov = [{"target": "t", "heuristic": h, "result": r} for h, r in
               [("boundary", "BLOCKED"), ("duplicate", "blocked (needs --faults)"), ("empty-null", "NOT_RUN"),
                ("keyboard-a11y", "skipped"), ("happy-path", "PASS"), ("invalid-format", "Failed")]]
        memory.ingest(self.con, run(coverage=cov))
        untried = memory.card(self.con, "/form").split("UNTRIED: ")[1].splitlines()[0]
        for h in ("boundary", "duplicate", "empty-null", "keyboard-a11y"):
            self.assertIn(h, untried)
        for h in ("happy-path", "invalid-format"):
            self.assertNotIn(h, untried)
        self.assertEqual({r[0] for r in self.con.execute("SELECT result FROM coverage")}, {"pass", "fail", "blocked"})

    def test_heuristic_ids_normalised(self):
        memory.ingest(self.con, run(coverage=[{"target": "x", "heuristic": "Boundary"},
                                          {"target": "y", "heuristic": "Traffic time zone"}]))
        c = memory.card(self.con, "/form")
        self.assertIn("other:traffic-time-zone", c)
        self.assertNotIn("boundary", c.split("UNTRIED: ")[1].splitlines()[0])

    def test_blocked_stays_untried_and_paraphrased_facts_collapse(self):
        memory.ingest(self.con, run(coverage=[{"target": "api", "heuristic": "error-handling", "result": "blocked"}],
                                facts=[{"kind": "rule", "text": "ID, Road and Type buttons sort ascending"}]), "2026-11-22T10:00:00Z")
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "ID, Road and Type header buttons sort ascending"}]),
                  "2026-11-22T11:00:00Z")
        c = memory.card(self.con, "/form")
        self.assertIn("error-handling", c.split("UNTRIED")[1])
        self.assertIn("header buttons", c)
        self.assertEqual(c.count("sort ascending"), 1)

    def test_distinct_rules_not_collapsed(self):
        facts = ["Name field is required", "Email field is required", "Start date must be before end date",
                 "Start time must be before end time", "Speed limit must be between 0 and 130 km/h",
                 "Speed limit must be between 0 and 120 km/h"]
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": t} for t in facts]))
        c = memory.card(self.con, "/form")
        for t in facts:
            self.assertIn(t, c)

    def test_ticket_fact_becomes_observed(self):
        memory.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "End date after start", "source": "PROJ-1"}]))
        self.assertIn("(from PROJ-1)", memory.card(self.con, "/form"))
        memory.ingest(self.con, run("1.0", facts=[{"kind": "rule", "text": "End date after start", "source": "observed"}]))
        self.assertNotIn("(from PROJ-1)", memory.card(self.con, "/form"))
        memory.ingest(self.con, run("unknown", facts=[{"kind": "rule", "text": "x", "source": "PROJ-2"}]))  # unverified stays 'from'
        self.assertIn("(from PROJ-2)", memory.card(self.con, "/form"))

    def test_size_caps(self):
        core.MAX_FACTS = 5
        try:
            for n in range(8):
                memory.ingest(self.con, run(facts=[{"kind": "note", "text": f"fact number {n} " + "w" * n}]), f"2026-11-22T10:0{n}:00Z")
            texts = [r[0] for r in self.con.execute("SELECT text FROM facts")]
            self.assertEqual(len(texts), 5)
            self.assertTrue(any("fact number 7" in t for t in texts))
            self.assertEqual(self.con.execute("SELECT count(*) FROM facts_fts").fetchone()[0], 5)
        finally:
            core.MAX_FACTS = 100

    def test_bad_shapes_rejected(self):
        for bad in ([], {"run_id": "x", "build": "1", "pages": {"route": "/a"}},
                    {"run_id": "x", "build": "1", "bugs": ["oops"]}, {"build": "1"},
                    {"run_id": "   ", "build": "1"},
                    {"run_id": "x", "pages": {}}, {"run_id": "x", "bugs": ""},
                    {"run_id": "x", "cases": False}, {"run_id": "x", "env": []},
                    {"run_id": "x", "questions": {}}, {"run_id": "x", "not_tested": ""},
                    {"run_id": "x", "bugs": [{"route": "http://[x", "title": "bad"}]},
                    {"run_id": "x", "bugs": [{"route": "/a", "title": "   "}]},
                    {"run_id": "x", "cases": [{"id": "TC-1", "status": "PASS", "annotated": "false"}]}):
            with self.assertRaises(ValueError, msg=str(bad)):
                memory.ingest(self.con, bad)

    def test_round1_memory_and_core_fixes(self):
        # 1. cap/norm/short preserve numeric 0; norm preserves non-ASCII
        self.assertEqual((core.cap(0), core.norm(0), core.short(0, 10)), ("0", "0", "0"))
        self.assertNotEqual(core.bug_hash("/a", "日本語エラー"), core.bug_hash("/a", "한국어오류"))
        self.assertEqual(core.norm("Erreur: échec"), "erreur échec")
        # 2. whitespace symptom falls back to title (distinct bugs don't collide)
        memory.ingest(self.con, {"run_id": "b1", "build": "1.0", "bugs": [
            {"route": "/a", "title": "Bug A", "symptom": "   "},
            {"route": "/a", "title": "Bug B", "symptom": "   "},
        ]})
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 2)
        # 3. unknown-build plan does not overwrite/un-stale verified locators or store blank locators/heuristics
        memory.ingest(self.con, {"run_id": "r1", "build": "1.0", "pages": [
            {"route": "/loc", "locators": [{"name": "Save", "locator": "#old"}]}]}, "2026-01-01T00:00:00Z")
        memory.ingest(self.con, {"run_id": "r2", "build": "2.0", "pages": [{"route": "/loc"}]}, "2026-01-02T00:00:00Z")
        memory.ingest(self.con, {"run_id": "p1", "build": "unknown", "pages": [
            {"route": "/loc", "locators": [{"name": "Save", "locator": "#guess"}, {"name": "  ", "locator": "  "}],
             "coverage": [{"target": "t", "heuristic": "   ", "result": "pass"}]}]}, "2026-01-03T00:00:00Z")
        rows = [dict(r) for r in self.con.execute("SELECT name, locator, build, stale FROM locators WHERE page='/loc'")]
        self.assertEqual(rows, [{"name": "Save", "locator": "#old", "build": "1.0", "stale": 1}])
        self.assertEqual(self.con.execute("SELECT count(*) FROM coverage WHERE page='/loc'").fetchone()[0], 0)
        # 4. fact dedup keeps numbers with attached units distinct
        memory.ingest(self.con, {"run_id": "u1", "build": "2.0", "pages": [{"route": "/units", "facts": [
            {"kind": "rule", "text": "Speed limit must be between 0 and 130km/h"},
            {"kind": "rule", "text": "Speed limit must be between 0 and 120km/h"},
        ]}]})
        cu = memory.card(self.con, "/units")
        self.assertIn("130km/h", cu)
        self.assertIn("120km/h", cu)
        # 5. search multi-word negation excludes only the phrase, not individual words
        memory.ingest(self.con, {"run_id": "s1", "build": "2.0", "pages": [{"route": "/s", "facts": [
            {"kind": "note", "text": "speed chart loads slowly"},
            {"kind": "rule", "text": "speed-limit max 130"},
            {"kind": "api", "text": "GET /api/incidents returns 500"},
            {"kind": "api", "text": "GET /api/zones returns 500"},
        ]}]})
        self.assertIn("speed chart", memory.search(self.con, "speed -speed-limit"))
        self.assertNotIn("speed-limit", memory.search(self.con, "speed -speed-limit"))
        self.assertIn("/api/incidents", memory.search(self.con, "500 -/api/zones"))
        self.assertNotIn("/api/zones", memory.search(self.con, "500 -/api/zones"))
        # 6. MAX_RUNS pruning leaves no orphan sightings
        orig_max = core.MAX_RUNS
        core.MAX_RUNS = 2
        try:
            memory.ingest(self.con, {"run_id": "m2", "build": "2.0", "bugs": []}, "2026-12-02T00:00:00Z")
            memory.ingest(self.con, {"run_id": "m3", "build": "2.0", "bugs": []}, "2026-12-03T00:00:00Z")
            memory.ingest(self.con, {"run_id": "m1", "build": "2.0", "bugs": [{"route": "/m", "title": "old"}]},
                          "2026-01-01T00:00:00Z", src="f1")
            orphans = self.con.execute("SELECT count(*) FROM sightings WHERE run_id NOT IN (SELECT id FROM runs)").fetchone()[0]
            self.assertEqual(orphans, 0)
        finally:
            core.MAX_RUNS = orig_max
        # 7. QA_MEM_DB expands ~ and empty QA_SDET_DATA / QA_SDET_CONFIG do not resolve to '.'
        with mock.patch.dict(os.environ, QA_MEM_DB="~/custom.db"):
            self.assertFalse(str(core.db_path()).startswith("~"))


class Card(MemBase):
    """`qa-mem card`: what an explorer reads about a page."""

    def test_card_shows_facts_locators_coverage_untried(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "Speed 0-130"}],
                                locators=[{"name": "Save", "locator": "getByRole('button')"}],
                                coverage=[{"target": "speed", "heuristic": "boundary", "result": "fail"}]))
        c = memory.card(self.con, "form")
        self.assertIn("Speed 0-130", c)
        self.assertIn("Save=getByRole('button')", c)
        self.assertIn("boundary(speed:fail)", c)
        untried = c.split("UNTRIED: ")[1].splitlines()[0]
        self.assertNotIn("boundary", untried)
        self.assertIn("happy-path", untried)

    def rules(self, page, *labels):
        with self.con:
            for i, label in enumerate(labels):
                self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (page, "rule", label, "max 10", f"src/F.jsx:{i}", "/repo", "frontend", core.now(), "src/F.jsx"))

    @staticmethod
    def untested(card):
        line = next((x for x in card.splitlines() if x.startswith("UNTESTED TARGETS")), "")
        return line.split("): ", 1)[1] if line else ""

    def test_card_lists_code_rule_fields_no_run_covered(self):
        """One boundary-tested field must not hide the page's other validated fields."""
        self.rules("/form", "speed", "name", "start date")
        memory.ingest(self.con, run(coverage=[{"target": "Speed limit field", "heuristic": "boundary", "result": "pass"}]))
        c = memory.card(self.con, "/form")
        self.assertEqual(self.untested(c), "name; start date")
        self.assertIn("covered on build 1.0", c)
        self.assertNotIn("boundary", c.split("UNTRIED: ")[1].splitlines()[0])  # the heuristic view is unchanged

    def test_untested_targets_match_one_way_and_ignore_blocked(self):
        self.rules("/form", "speed", "speed (2)", "start date", "name")
        memory.ingest(self.con, run(coverage=[{"target": "date", "heuristic": "boundary", "result": "pass"},
                                              {"target": "name", "heuristic": "empty-null", "result": "blocked"}]))
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "name; speed; start date")  # 'speed (2)': one field
        memory.ingest(self.con, run(coverage=[{"target": "speed", "heuristic": "boundary", "result": "fail"}]))
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "name; start date")

    def row(self, page, kind, label, value, src="src/F.jsx"):
        with self.con:
            self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                             (page, kind, label, value, f"{src}:1", "/repo", "frontend" if page != "*" else "backend",
                              core.now(), src))

    def test_only_an_adversarial_check_clears_a_rule(self):  # a happy-path save of a valid speed proved nothing
        self.rules("/form", "speed")
        memory.ingest(self.con, run(coverage=[{"target": "speed", "heuristic": "happy-path", "result": "pass"}]))
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "speed")
        memory.ingest(self.con, run(coverage=[{"target": "speed", "heuristic": "invalid-format", "result": "pass"}]))
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "")

    def test_linked_server_rules_are_targets_and_a_client_server_mismatch_is_hinted(self):
        self.row("/form", "api", "save", "POST /api/zones")
        self.row("/form", "rule", "speed", "max 130")
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        self.row("*", "rule", "POST /api/zones speed", "max 150", "api/zones.js")
        memory.ingest(self.con, run())
        c = memory.card(self.con, "/form")
        self.assertIn("[server] POST /api/zones speed", self.untested(c))
        hint = next(x for x in c.splitlines() if x.startswith("CLIENT/SERVER"))
        self.assertIn("speed", hint)
        self.assertIn("max 130", hint)
        self.assertIn("max 150", hint)

    def test_camel_case_server_fields_and_negative_bounds_are_compared(self):
        self.row("/zones/new", "api", "save", "POST /api/zones")
        self.row("/zones/new", "rule", "speed", "min 0, max 130 (react-hook-form register)")
        self.row("/zones/new", "rule", "offset", "min -5, max 5")
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        self.row("*", "rule", "POST /api/zones speedLimit", "integer, min 0, max 150, required (Joi)", "api/zones.js")
        self.row("*", "rule", "POST /api/zones offset", "min 5, max 5", "api/zones.js")  # -5 vs 5: the minus counts
        memory.ingest(self.con, {"run_id": "z", "build": "1", "pages": [{"route": "/zones/new"}]})
        differ = memory._client_server_differ(self.con, "/zones/new")
        self.assertEqual(len(differ), 2, differ)
        self.assertTrue(any(d.startswith("speed:") and "max 150" in d for d in differ), differ)
        self.assertTrue(any(d.startswith("offset:") and "min -5" in d for d in differ), differ)

    def test_equal_camel_case_bounds_and_range_text_are_not_a_mismatch(self):
        self.row("/zones/new", "api", "save", "POST /api/zones")
        self.row("/zones/new", "rule", "speed", "0-130 km/h")
        self.row("/zones/new", "rule", "start date", "min 2024-01-01")
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        self.row("*", "rule", "POST /api/zones speedLimit", "integer, min 0, max 130, required (Joi)", "api/zones.js")
        self.row("*", "rule", "POST /api/zones startDate", "min 2024-01-01", "api/zones.js")
        memory.ingest(self.con, {"run_id": "z", "build": "1", "pages": [{"route": "/zones/new"}]})
        self.assertEqual(memory._client_server_differ(self.con, "/zones/new"), [])

    def test_a_server_check_does_not_clear_the_page_field_of_the_same_name(self):
        self.row("/form", "api", "save", "POST /api/zones")
        self.row("/form", "rule", "speed", "max 130")
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js")
        self.row("*", "rule", "POST /api/zones speed", "max 150", "api/zones.js")
        memory.ingest(self.con, run(coverage=[{"target": "[server] POST /api/zones speed", "heuristic": "boundary", "result": "pass"}]))
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "speed")  # the form's own field is still untested
        memory.ingest(self.con, run(coverage=[{"target": "POST /api/zones speed", "heuristic": "boundary", "result": "pass"}]))
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "speed")  # also without the [server] prefix
        memory.ingest(self.con, run(coverage=[{"target": "speed", "heuristic": "boundary", "result": "pass"}]))
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "")  # the form's check clears it; the server rule was checked

    def test_untested_targets_reset_on_a_new_build(self):
        self.rules("/form", "speed")
        memory.ingest(self.con, run("1.0", coverage=[{"target": "speed", "heuristic": "boundary"}]), "2026-01-01T00:00:00Z")
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "")
        memory.ingest(self.con, run("2.0"), "2026-01-02T00:00:00Z")
        self.assertEqual(self.untested(memory.card(self.con, "/form")), "speed")

    def test_untested_targets_line_is_capped_and_absent_without_rules(self):
        memory.ingest(self.con, run())
        self.assertNotIn("UNTESTED TARGETS", memory.card(self.con, "/form"))
        labels = [f"field {i:02} " + "x" * 41 for i in range(12)]  # 50 characters each
        self.rules("/form", *labels)
        shown, more = self.untested(memory.card(self.con, "/form")).split("; +")
        self.assertEqual(shown.split("; "), labels[:7])  # whole labels, at most ~400 characters
        self.assertEqual(more, "5 more (qa-mem code '/form')")

    def test_untested_targets_on_a_code_only_page_name_no_build(self):
        self.rules("/zones/new", "speed")
        self.con.execute("INSERT INTO pages(route) VALUES('/zones/new')")
        line = next(x for x in memory.card(self.con, "/zones/new").splitlines() if x.startswith("UNTESTED TARGETS"))
        self.assertIn("covered on this build", line)
        self.assertNotIn("None", line)

    def test_unknown_page_card(self):
        self.assertIn("NO CARD", memory.card(self.con, "/nope"))

    def test_card_prefers_exact_then_shortest_route(self):
        for rt in ("/form/advanced", "/form"):
            memory.ingest(self.con, {"run_id": rt, "build": "1", "pages": [{"route": rt}]})
        self.assertIn("PAGE /form ", memory.card(self.con, "form"))

    def test_route_query_is_exact(self):
        memory.ingest(self.con, {"run_id": "a", "build": "1", "pages": [{"route": "/incidents/list"}]})
        self.assertIn("NO CARD", memory.card(self.con, "/incidents"))
        self.assertIn("matched from 'incidents'", memory.card(self.con, "incidents"))

    def test_page_name_lookup_prefers_segment_and_exact_name(self):
        for route, name in [("/roadmap", "Roadmap"), ("/map.html", "Live map"), ("/zones", "Speed zones"), ("/z", "Zones")]:
            memory.ingest(self.con, {"run_id": route, "build": "1", "pages": [{"route": route, "name": name}]})
        self.assertIn("PAGE /map.html", memory.card(self.con, "map"))
        self.assertIn("PAGE /zones ", memory.card(self.con, "zones"))  # the route itself first
        self.assertIn("PAGE /map.html", memory.card(self.con, "live map"))  # then the exact page name

    def test_detail_routes_share_one_card(self):
        self.assertEqual(core.norm_route("http://h/incidents/4521/?x=1"), "/incidents/:id")
        self.assertEqual(core.norm_route("/#/zones/12/"), "/#/zones/:id")
        self.assertEqual(core.norm_route("/v2/a//b"), "/v2/a/b")
        self.assertEqual(core.norm_route("https://qa.example.com/#!/incidents/4521"), "/#!/incidents/:id")
        self.assertNotEqual(core.norm_route("/#!/incidents"), core.norm_route("/#!/zones"))
        self.assertEqual(core.norm_route("/app#section"), "/app")
        for r in ['"I #5ge', "a b ?x", "/x/ 12 /y#/z ?q"]:  # idempotent, so a stored route finds itself
            self.assertEqual(core.norm_route(core.norm_route(r)), core.norm_route(r))
        memory.ingest(self.con, {"run_id": "d", "build": "1", "pages": [{"route": "/incidents/4521",
                  "facts": [{"kind": "rule", "text": "detail shows road"}]}]})
        self.assertIn("detail shows road", memory.card(self.con, "/incidents/77"))
        self.assertEqual(core.bug_hash(core.norm_route("/incidents/1"), "x"), core.bug_hash(core.norm_route("/incidents/2"), "x"))

    def test_route_spellings_find_the_same_card(self):
        memory.ingest(self.con, run())
        for q in ("/form/", "/form?x=1", "http://localhost:8765/form"):
            self.assertIn("PAGE /form ", memory.card(self.con, q), q)
        self.assertEqual(core.norm_route("/#/map"), "/#/map")
        with self.assertRaises(ValueError):
            memory.ingest(self.con, run(bugs=[{"title": "no route"}]))

    def test_unparseable_url_is_no_route_not_a_traceback(self):
        self.assertEqual(core.norm_route("http://[x"), "")
        self.assertIn("NO CARD", memory.card(self.con, "http://[x"))
        with self.assertRaises(ValueError):
            memory.ingest(self.con, {"run_id": "r", "pages": [{"route": "http://[x"}]})

    def test_card_says_when_verified_locators_did_not_fit(self):
        locs = [{"name": f"btn{i}", "locator": f"getByRole('button', {{ name: 'Button number {i} {'x' * 60}' }})"}
                for i in range(25)]
        memory.ingest(self.con, run("1.0", locators=locs))
        c = memory.card(self.con, "/form")
        self.assertRegex(c, r"\(\+\d+ more verified")

    def fixed_bug(self, draft="/proj/qa-out/run/bugs/BUG-1.md"):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "speed 999 saved",
                                           "draft_path": draft}]), "2026-01-01T00:00:00Z")
        h = core.bug_hash("/form", "speed 999 saved")
        cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        return h

    @staticmethod
    def fixed_line(card, h):
        return next(x for x in card.split("PREVIOUSLY FIXED", 1)[1].splitlines() if x.startswith(f"- {h} "))

    def test_fixed_bug_drafts_are_listed_by_the_bugs_command(self):
        """The explorer re-checks a fixed bug by its draft's steps: `qa-mem bugs FIXED` names the draft."""
        self.fixed_bug()
        self.assertIn("/proj/qa-out/run/bugs/BUG-1.md", memory.bugs(self.con, "FIXED"))

    def test_fixed_bug_rechecked_on_this_build_is_marked(self):
        h = self.fixed_bug(draft=None)
        memory.ingest(self.con, run(coverage=[{"target": h, "heuristic": "regression", "result": "pass"}]),
                      "2026-01-02T00:00:00Z")
        c = memory.card(self.con, "/form")
        self.assertTrue(self.fixed_line(c, h).endswith(" | re-checked on build 1.0"), c)
        self.assertIn(f"other:regression({h}:pass)", c)
        self.assertEqual(c.split("UNTRIED: ")[1].splitlines()[0], ", ".join(core.HEURISTICS))  # regression is no heuristic
        memory.ingest(self.con, run("2.0"), "2026-01-03T00:00:00Z")  # a new build: check it again
        self.assertNotIn("re-checked", self.fixed_line(memory.card(self.con, "/form"), h))

    def test_a_newly_fixed_older_bug_is_on_the_card_gaps_counts(self):
        """bug-status and bug-sync don't touch last_seen: an older bug fixed today must still be listed to re-check."""
        memory.ingest(self.con, run("2.0", bugs=[{"route": "/form", "title": "old", "symptom": "old bug"}]),
                      "2026-01-01T00:00:00Z")
        h_old = core.bug_hash("/form", "old bug")
        for i in range(6):
            s = f"newer {i}"
            memory.ingest(self.con, run("2.0", bugs=[{"route": "/form", "title": s, "symptom": s}]), f"2026-01-0{i + 2}T00:00:00Z")
            h = core.bug_hash("/form", s)
            cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
            memory.ingest(self.con, run("2.0", coverage=[{"target": h, "heuristic": "regression", "result": "pass"}]),
                          f"2026-01-0{i + 2}T01:00:00Z")
        cli.run_cmd(self.con, "bug-status", [h_old, "FIXED"])
        self.assertIn("1 fixed bug to re-check", memory.gaps(self.con))
        c = memory.card(self.con, "/form")
        self.assertFalse(self.fixed_line(c, h_old).endswith("re-checked on build 2.0"), c)
        self.assertIn("(+2 more FIXED: qa-mem bugs FIXED)", c)

    def test_a_recheck_on_an_unreadable_build_counts_for_a_week(self):
        """Like coverage: a re-check from a run that couldn't read the build holds for 7 days, then is due again."""
        memory.ingest(self.con, run("unknown", bugs=[{"route": "/form", "title": "t", "symptom": "s"}]), core.now())
        h = core.bug_hash("/form", "s")
        cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        memory.ingest(self.con, run("unknown", coverage=[{"target": h, "heuristic": "regression", "result": "pass"}]), core.now())
        self.assertEqual(memory._unrechecked_fixed(self.con, "/form", core.UNKNOWN), 0)
        self.assertTrue(self.fixed_line(memory.card(self.con, "/form"), h).endswith("| re-checked"))
        self.con.execute("UPDATE coverage SET ts='2020-01-01T00:00:00Z'")
        self.assertEqual(memory._unrechecked_fixed(self.con, "/form", core.UNKNOWN), 1)

    def test_card_size_is_bounded(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": f"fact {i} " + "y" * 280} for i in range(30)],
                                locators=[{"name": f"loc{i} " + "n" * 250, "locator": "x" * 290} for i in range(30)],
                                coverage=[{"heuristic": h, "target": f"t{i} " + "z" * 280} for h in core.HEURISTICS for i in range(6)],
                                bugs=[{"route": "/form", "title": "b" * 300, "symptom": f"s{i} " + "q" * 280,
                                       "draft_path": "/d/" + "p" * 2000} for i in range(12)]))
        with self.con:
            self.con.execute("UPDATE bugs SET status='FIXED' WHERE rowid IN (SELECT rowid FROM bugs LIMIT 6)")
        self.rules("/form", *(f"rule {i:02} " + "r" * 110 for i in range(12)))
        for page in ("/form", "/other"):  # another page on the same endpoint, with bugs of its own
            self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                             (page, "api", "save", "POST /api/x", "a.js:1", "/repo", "frontend", core.now(), "a.js"))
        memory.ingest(self.con, {"run_id": "other", "build": "1.0", "pages": [{"route": "/other"}], "bugs": [
            {"route": "/other", "title": "o" * 300, "symptom": f"o{i} " + "q" * 280} for i in range(8)]})
        c = memory.card(self.con, "/form")
        self.assertIn("BUGS ON PAGES SHARING AN ENDPOINT", c)
        self.assertLess(len(c), 15000)  # ~3.5k tokens at worst


class Gaps(MemBase):
    """`qa-mem gaps`: every page, the most urgent to explore first."""

    def row(self, page, kind, label, value, src, repo="/repo", area="frontend"):
        with self.con:
            if page != "*":
                self.con.execute("INSERT OR IGNORE INTO pages(route) VALUES(?)", (page,))
            self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                             (page, kind, label, value, f"{src}:1", repo, area, core.now(), src))

    def visit(self, route, build, ts):
        memory.ingest(self.con, {"run_id": f"r-{route}-{ts}", "build": build, "pages": [{"route": route}]}, ts)

    @staticmethod
    def lines(out):
        return [x for x in out.splitlines() if x.startswith("- ")]

    def commit(self, repo, rel, when):
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = when  # restored by the sandbox
        (repo / rel).write_text(when)
        git(repo, "commit", "-qam", when)

    def test_no_pages_is_a_hint(self):
        self.assertIn("/qa-sdet:qa-init", memory.gaps(self.con))

    def test_order_never_explored_then_old_build_then_the_rest(self):
        self.row("/new", "rule", "speed", "max 130", "src/New.jsx")
        self.visit("/old", "1.0", "2026-01-01T00:00:00Z")
        self.visit("/cur", "2.0", "2026-01-02T00:00:00Z")
        self.visit("/unread", "unknown", "2026-01-03T00:00:00Z")
        out = memory.gaps(self.con)
        self.assertIn("current build 2.0", out.splitlines()[0])
        lines = self.lines(out)
        self.assertTrue(lines[0].startswith('- /new "": never explored; 1 untested field'), lines)
        self.assertTrue(lines[1].startswith('- /old "": verified on build 1.0: re-verify; 15 untried'), lines)
        self.assertEqual({x.split()[1] for x in lines[2:]}, {"/cur", "/unread"})
        self.assertIn("build never read", next(x for x in lines if "/unread" in x))
        self.assertTrue(out.rstrip().endswith("Next: /qa-sdet:qa-explore '/new'"), out)

    def test_source_changed_since_the_last_visit(self):
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        repo = git_repo(self.t / "app", {"src/Form.jsx": "a", "src/Other.jsx": "b", "api/zones.js": "c"})
        rp = os.path.realpath(repo)
        self.row("/form", "api", "save", "POST /api/zones", "src/Form.jsx", rp)
        self.row("*", "api", "POST /api/zones", "create zone", "api/zones.js", rp, "backend")
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")  # after the first commit: nothing changed since
        self.commit(repo, "src/Other.jsx", "2026-01-06T00:00:00Z")  # another page's file
        self.assertNotIn("source changed", memory.gaps(self.con))
        self.commit(repo, "api/zones.js", "2026-01-07T00:00:00Z")  # the backend of an endpoint the page calls
        self.assertIn("source changed (api/zones.js)", memory.gaps(self.con))
        self.commit(repo, "src/Form.jsx", "2026-01-08T00:00:00Z")
        self.assertIn("source changed (api/zones.js, +1)", memory.gaps(self.con))

    def test_a_change_inside_a_submodule_counts(self):
        """The scan maps a submodule's files under the parent repo; the parent's log names only the submodule folder."""
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        kit = git_repo(self.t / "kit", {"Button.jsx": "a"})
        app = git_repo(self.t / "app", {"src/Form.jsx": "a"})
        git(app, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(kit), "lib/kit")
        git(app, "commit", "-qm", "add kit")
        self.row("/form", "locator", "Save", "getByLabel('Save')", "lib/kit/Button.jsx", os.path.realpath(app))
        self.row("/other", "locator", "Name", "getByLabel('Name')", "lib/kitchen.js", os.path.realpath(app))
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")
        self.visit("/other", "1.0", "2026-01-05T00:00:00Z")
        self.commit(kit, "Button.jsx", "2026-01-06T00:00:00Z")
        git(app / "lib/kit", "fetch", "-q")
        git(app / "lib/kit", "checkout", "-q", "FETCH_HEAD")
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-07T00:00:00Z"
        git(app, "commit", "-qam", "bump kit")  # the parent's log lists only 'lib/kit'
        out = memory.gaps(self.con)
        self.assertIn("source changed (lib/kit/Button.jsx)", next(x for x in out.splitlines() if x.startswith("- /form ")))
        self.assertNotIn("source changed", next(x for x in out.splitlines() if x.startswith("- /other ")))  # 'lib/kitchen.js'

    def test_source_changes_on_a_page_whose_build_was_never_read(self):
        """No last_verified (build unreadable): the last run on the page is the visit to compare commits against."""
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        repo = git_repo(self.t / "app", {"src/Form.jsx": "a"})
        self.row("/form", "rule", "speed", "max 130", "src/Form.jsx", os.path.realpath(repo))
        memory.ingest(self.con, {"run_id": "r1", "target": "http://qa/form", "build": "unknown",
                                 "pages": [{"route": "/form"}]}, "2026-01-05T00:00:00Z")
        self.assertNotIn("source changed", memory.gaps(self.con))
        self.commit(repo, "src/Form.jsx", "2026-01-06T00:00:00Z")
        self.assertIn("source changed (src/Form.jsx)", memory.gaps(self.con))

    def test_routes_with_an_id_are_not_batch_picks(self):
        """'/incidents/:id' can't be opened as is: gaps marks it, Next: and --json skip it."""
        self.row("/incidents/:id", "rule", "title", "required", "src/Detail.jsx")
        self.row("/incidents", "rule", "q", "max 50", "src/List.jsx")
        out = memory.gaps(self.con)
        self.assertIn("(needs a record id)", next(x for x in self.lines(out) if "/incidents/:id" in x))
        self.assertTrue(out.rstrip().endswith("Next: /qa-sdet:qa-explore '/incidents'"), out)
        data = json.loads(memory.gaps(self.con, top=1, as_json=True))
        self.assertEqual(([p["route"] for p in data["pages"]], data["needs_id"]), (["/incidents"], ["/incidents/:id"]))

    def test_the_card_names_what_changed_since_the_last_visit(self):
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        repo = git_repo(self.t / "app", {"src/Form.jsx": "a"})
        self.row("/form", "rule", "speed", "max 130", "src/Form.jsx", os.path.realpath(repo))
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")
        self.assertNotIn("CHANGED SINCE", memory.card(self.con, "/form"))
        self.commit(repo, "src/Form.jsx", "2026-01-06T00:00:00Z")
        line = next(x for x in memory.card(self.con, "/form").splitlines() if x.startswith("CHANGED SINCE"))
        self.assertIn("src/Form.jsx", line)
        self.assertIn("speed", line)  # the rule defined in the changed file

    def test_tiers_changed_new_with_forms_old_build_current_and_history_breaks_ties(self):
        self.row("/static", "locator", "Help", "getByText('Help')", "src/Help.jsx")  # never explored, nothing to submit
        self.row("/new-form", "rule", "speed", "max 130", "src/New.jsx")  # never explored, a validated form
        self.visit("/old", "1.0", "2026-01-01T00:00:00Z")
        for route in ("/quiet", "/fragile", "/regressed"):
            self.visit(route, "2.0", "2026-01-02T00:00:00Z")
        memory.ingest(self.con, {"run_id": "b", "build": "2.0", "pages": [{"route": "/fragile"}],
                                 "bugs": [{"route": "/fragile", "title": f"t{i}", "symptom": f"s{i}", "severity": "HIGH"}
                                          for i in range(2)]}, "2026-01-02T00:00:00Z")
        memory.ingest(self.con, {"run_id": "c", "build": "2.0", "pages": [{"route": "/regressed"}],
                                 "bugs": [{"route": "/regressed", "title": "r", "symptom": "r"}]}, "2026-01-02T00:00:00Z")
        self.con.execute("UPDATE bugs SET status='REGRESSED' WHERE page='/regressed'")
        self.con.execute("UPDATE bugs SET status='FILED' WHERE page='/fragile'")
        routes = [p["route"] for p in json.loads(memory.gaps(self.con, as_json=True))["pages"]]
        self.assertEqual(routes[:3], ["/regressed", "/new-form", "/old"])
        self.assertLess(routes.index("/fragile"), routes.index("/quiet"))  # 2 HIGH bugs: a hotspot first in its tier
        self.assertEqual(routes[-1], "/static")
        info = json.loads(memory.gaps(self.con, as_json=True))["pages"][0]
        self.assertEqual((info["tier"], info["hotspot"] >= 0), ("changed or regressed", True))

    def test_a_repo_git_cannot_read_is_named_not_fatal(self):
        self.row("/form", "rule", "speed", "max 130", "src/Form.jsx", "/nowhere/repo")
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")
        out = memory.gaps(self.con)
        self.assertIn("/form", out)
        self.assertIn("source changes not checked for /nowhere/repo", out)

    def test_cli(self):
        self.visit("/form", "1.0", "2026-01-05T00:00:00Z")
        code, out, _ = qa_mem("gaps")
        self.assertEqual(code, 0)
        self.assertIn("/form", out)

    def test_gaps_changed_top_json_and_unrechecked_fixed(self):
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2026-01-01T00:00:00Z"
        repo = git_repo(self.t / "app", {"src/A.jsx": "a", "src/B.jsx": "b", "src/C.jsx": "c"})
        rp = os.path.realpath(repo)
        for route, src in (("/a", "src/A.jsx"), ("/b", "src/B.jsx"), ("/c", "src/C.jsx")):
            self.row(route, "rule", "x", "req", src, rp)
            self.visit(route, "1.0", "2026-01-05T00:00:00Z")
        # Before any source changes: --changed reports none cleanly
        code, out, _ = qa_mem("gaps", "--changed")
        self.assertEqual(code, 0)
        self.assertIn("no pages with source changes", out)
        code, jout, _ = qa_mem("gaps", "--changed", "--json")
        self.assertEqual((code, json.loads(jout)["pages"]), (0, []))
        # Modify A and C; mark a FIXED bug on B (un-rechecked)
        self.commit(repo, "src/A.jsx", "2026-01-06T00:00:00Z")
        self.commit(repo, "src/C.jsx", "2026-01-07T00:00:00Z")
        memory.ingest(self.con, {"run_id": "fb", "build": "1.0", "pages": [{"route": "/b"}],
                                 "bugs": [{"route": "/b", "title": "old", "symptom": "old"}]}, "2026-01-05T00:00:00Z")
        h = core.bug_hash("/b", "old")
        self.con.execute("UPDATE bugs SET status='FIXED' WHERE hash=?", (h,))
        self.con.commit()
        code, jout, _ = qa_mem("gaps", "--changed", "--json")
        data = json.loads(jout)
        self.assertEqual(code, 0)
        self.assertEqual([p["route"] for p in data["pages"]], ["/a", "/c"])
        self.assertTrue(all(p["changed"] for p in data["pages"]))
        code, jtop, _ = qa_mem("gaps", "--top", "2", "--json")
        self.assertEqual((code, len(json.loads(jtop)["pages"])), (0, 2))
        code, jall, _ = qa_mem("gaps", "--json")
        by_route = {p["route"]: p for p in json.loads(jall)["pages"]}
        self.assertEqual(by_route["/b"]["unrechecked_fixed"], 1)
        self.assertIn("1 fixed bug to re-check", qa_mem("gaps")[1])
        for bad in ("0", "-2", "abc"):
            self.assertEqual(qa_mem("gaps", "--top", bad)[0], 2, bad)


class Search(MemBase):
    """`qa-mem search`."""

    def test_text_capped_and_fts_search(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "closure schedule " + "x" * 500}]))
        t = self.con.execute("SELECT text FROM facts").fetchone()[0]
        self.assertEqual(len(t), core.MAX_TEXT)
        self.assertIn("closure", memory.search(self.con, "closure"))
        self.assertFalse(memory.search(self.con, "closure").endswith("*"))
        memory.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "other"}]))  # new build stales old facts
        self.assertTrue(memory.search(self.con, "closure").endswith(" *"))

    def test_search_with_broken_fts_syntax_falls_back_to_words(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "I-5 N closure: lane 2 (a-b) 50% done"}]))
        for q in ["closure AND", "NEAR(closure", "a-b", "I-5", "closure lane"]:
            self.assertIn("closure", memory.search(self.con, q), q)
        self.assertIn("no facts match", memory.search(self.con, "*"))

    def test_operators_in_the_query_are_words(self):
        memory.ingest(self.con, run("1.43.0", facts=[{"kind": "rule", "text": "speed max 130"},
                                                  {"kind": "rule", "text": "closure AND detour shown"}]))
        self.assertNotIn("max 130", memory.search(self.con, "speed -130"))
        self.assertIn("detour", memory.search(self.con, "closure NEAR detour"))
        self.assertIn("max 130", memory.search(self.con, 'speed "130'))

    def test_symbols_in_the_query(self):
        memory.ingest(self.con, run(facts=[{"kind": "api", "text": "GET /api/zones returns 500 on empty filter"},
                                       {"kind": "note", "text": "speed chart loads slowly"}]))
        self.assertIn("returns 500", memory.search(self.con, "/api/zones"))
        self.assertIn("no facts match", memory.search(self.con, "speed-limit"))
        self.assertIn("speed chart", memory.search(self.con, "speed -/api/zones"))


class Retrieval(MemBase):
    """Stemmed search over facts and bugs, a relaxed match when nothing matches all words, and card ranking/focus."""

    def test_word_forms_match_and_bugs_are_searched(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "Corridor dates display in UTC"}],
                                    bugs=[{"route": "/form", "title": "Zone validation skipped", "symptom": "zones saved unvalidated"}]))
        self.assertIn("Corridor dates", memory.search(self.con, "date"))  # 'date' finds 'dates'
        out = memory.search(self.con, "zone validate")
        self.assertIn("[bug:OPEN]", out)
        self.assertIn("Zone validation skipped", out)
        h = core.bug_hash("/form", "zones saved unvalidated")
        cli.run_cmd(self.con, "bug-status", [h, "FILED", "PROJ-9"])
        self.assertIn("[bug:FILED PROJ-9]", memory.search(self.con, "proj-9"))  # the update reached the index

    def test_no_match_of_all_words_relaxes_to_two_of_them_and_says_so(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "Corridor dates display in UTC instead of local time"}]))
        out = memory.search(self.con, "date filter timezone UTC")
        self.assertIn("Corridor dates", out)
        self.assertIn("relaxed", out)
        self.assertIn("no facts match", memory.search(self.con, "date filter"))  # two words: no relaxing
        self.assertIn("no facts match", memory.search(self.con, "date filter UTC -corridor"))  # exclusions still hold

    def test_an_old_database_index_is_upgraded_in_place(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "Corridor dates display in UTC"}]))
        self.con.executescript("DROP TRIGGER facts_ai; DROP TRIGGER facts_ad; DROP TABLE facts_fts; "
                               "CREATE VIRTUAL TABLE facts_fts USING fts5(text, content='facts', content_rowid='id'); "
                               "INSERT INTO facts_fts(facts_fts) VALUES('rebuild'); DELETE FROM meta WHERE key='fts';")
        self.con.close()
        self.con = core.connect()
        self.assertIn("porter", self.con.execute("SELECT sql FROM sqlite_master WHERE name='facts_fts'").fetchone()[0])
        self.assertIn("Corridor dates", memory.search(self.con, "date"))

    def test_rules_outrank_newer_notes_and_a_focus_comes_first(self):
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": "date filter uses UTC"}]), "2026-01-01T00:00:00Z")
        memory.ingest(self.con, run(facts=[{"kind": "note", "text": f"layout note {i} {'x' * i}"} for i in range(20)]),
                      "2026-01-02T00:00:00Z")
        facts = lambda c: c.split("FACTS", 1)[1].split("\n")[1:]
        self.assertTrue(any("date filter uses UTC" in x for x in facts(memory.card(self.con, "/form"))))  # not evicted by notes
        memory.ingest(self.con, run(facts=[{"kind": "rule", "text": f"rule {i} {'y' * i}"} for i in range(20)]),
                      "2026-01-03T00:00:00Z")
        self.assertIn("date filter uses UTC", facts(memory.card(self.con, "/form", focus="date filters"))[0])
        code, out, _ = qa_mem("card", "/form", "--focus", "date filters")
        self.assertEqual(code, 0)
        self.assertIn("date filter uses UTC", out)


class Curation(MemBase):
    """Questions answered once stay answered (DECISIONS); the user's own facts (remember, pinned) and clean-up (forget)."""

    def test_an_answered_question_becomes_a_decision_on_the_card(self):
        q = "Is 130 inclusive?"
        memory.ingest(self.con, {**run(), "questions": [q, "Units?"]}, "2026-01-01T00:00:00Z")
        c = memory.card(self.con, "/form")
        self.assertIn("OPEN QUESTIONS", c)
        self.assertIn(q, c.split("OPEN QUESTIONS")[1])
        memory.ingest(self.con, {**run(), "questions": [{"text": q, "status": "resolved", "answer": "Yes, 0..130"}]},
                      "2026-01-02T00:00:00Z")
        memory.ingest(self.con, {**run(), "questions": [q]}, "2026-01-03T00:00:00Z")  # asked again: stays answered
        c = memory.card(self.con, "/form")
        self.assertIn(f"- {q} → Yes, 0..130", c.split("DECISIONS")[1])
        qid = self.con.execute("SELECT id FROM questions WHERE text='Units?'").fetchone()[0]
        self.assertEqual(qa_mem("answer", str(qid), "km/h. Authorization: Bearer abcdefghijklmnopqrst")[0], 0)
        out = qa_mem("questions", "resolved")[1]
        self.assertIn("Units? → km/h", out)
        self.assertNotIn("abcdefghijklmnopqrst", out)
        self.assertEqual(qa_mem("questions", "open")[1].strip(), "no open questions")
        self.assertEqual(qa_mem("answer", "999", "x")[0], 1)

    def test_plan_questions_are_remembered_too(self):
        d = Path(self.tmp.name) / "qa-out/r/.data"
        d.mkdir(parents=True)
        (d / "plan.json").write_text(json.dumps({"kind": "ticket", "tickets": [{"key": "P-1"}], "cases": [],
                                                 "questions": [{"text": "Q1 [BLOCKING] Is 0 valid?", "status": "resolved",
                                                                "answer": "No", "ticket": "PROJ-1"}]}))
        memory.ingest_pending(d.parent.parent)
        self.assertEqual(tuple(self.con.execute("SELECT page, ticket, status, answer FROM questions").fetchone()),
                         ("*", "PROJ-1", "resolved", "No"))
        self.assertIn("Is 0 valid? → No (PROJ-1)", memory.ticket_context(self.con, "Speed rules for PROJ-1")["decisions"][0])

    def test_remembered_facts_are_pinned_and_forget_cleans_up(self):
        memory.ingest(self.con, run("1.0"))  # the page is on build 1.0
        code, out, _ = qa_mem("remember", "/form", "flow", "Create a corridor first. Authorization: Bearer abcdefghijklmnopqrst")
        self.assertEqual(code, 0)
        qa_mem("remember", "/form", "note", "Header is blue", "--build-scoped")
        self.assertEqual(qa_mem("remember", "/form", "wish", "x")[0], 2)
        memory.ingest(self.con, run("2.0", facts=[{"kind": "note", "text": f"n{i} {'z' * i}"} for i in range(110)]))
        c = memory.card(self.con, "/form")
        pinned = next(x for x in c.splitlines() if "Create a corridor first" in x)
        self.assertIn("[pinned]", pinned)
        self.assertFalse(pinned.endswith("*"))  # a new build doesn't make it stale; the 100-fact cap kept it
        self.assertNotIn("abcdefghijklmnopqrst", c)
        self.assertIsNone(self.con.execute("SELECT 1 FROM facts WHERE text='Header is blue' AND stale=0").fetchone())
        fid = self.con.execute("SELECT id FROM facts WHERE text LIKE 'Create a corridor%'").fetchone()[0]
        self.assertIn(f"#{fid}", qa_mem("card", "/form", "--ids")[1])
        self.assertEqual(qa_mem("forget", "fact", str(fid))[0], 0)
        self.assertNotIn("Create a corridor", memory.card(self.con, "/form"))
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]))
        code, out, _ = qa_mem("forget", "page", "/form")
        self.assertEqual(code, 1)
        self.assertIn("open bug", out)
        self.assertEqual(qa_mem("forget", "page", "/form", "--force")[0], 0)
        self.assertIsNone(self.con.execute("SELECT 1 FROM pages WHERE route='/form'").fetchone())
        self.assertEqual(self.con.execute("SELECT count(*) FROM facts WHERE page='/form'").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 1)  # bug history stays


    def test_forget_page_also_drops_its_states_transitions_signals_and_questions(self):
        ts = core.now()
        memory.ingest(self.con, run())
        memory.ingest(self.con, {"run_id": "o", "build": "1", "pages": [{"route": "/other"}]})
        with self.con:
            for page in ("/form", "/other"):
                self.con.execute("INSERT INTO page_states VALUES(?,?,?,?,?,?,?)", (page, "tab: A", "tab", "click A", "unvisited", "1", ts))
                self.con.execute("INSERT INTO signals(page,kind,sig_key) VALUES(?,?,?)", (page, "request", "GET /x"))
                self.con.execute("INSERT INTO questions(page,text) VALUES(?,?)", (page, "Is 0 valid?"))
            self.con.execute("INSERT INTO transitions VALUES('/form','/other','link','/other','link','1',?)", (ts,))
            self.con.execute("INSERT INTO transitions VALUES('/other','/form','link','/form','link','1',?)", (ts,))
            self.con.execute("INSERT INTO transitions VALUES('/other','/third','link','/third','link','1',?)", (ts,))
        self.assertEqual(memory.forget_page(self.con, "/form")[0], True)
        for table, col in (("page_states", "page"), ("signals", "page"), ("questions", "page")):
            self.assertEqual([r[0] for r in self.con.execute(f"SELECT {col} FROM {table}")], ["/other"], table)
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT from_route, to_route FROM transitions")], [("/other", "/third")])


class Patterns(MemBase):
    """`qa-mem patterns`: how this app breaks again and again, from the heuristic and pattern each bug was recorded with."""

    def bug(self, page, symptom, **extra):
        memory.ingest(self.con, {"run_id": f"r-{page}-{symptom}", "target": page, "build": "1", "pages": [{"route": page}],
                                 "bugs": [{"route": page, "title": symptom, "symptom": symptom, **extra}]})

    def test_patterns_across_pages_rank_and_show_on_cards_and_in_prep(self):
        for page in ("/incidents", "/zones", "/closures"):
            self.bug(page, f"{page} shows 0 items on a 500", heuristic="error-handling", pattern="Silent error as EMPTY!")
        self.bug("/zones", "speed 131 saved", heuristic="boundary")
        self.bug("/form", "label typo", heuristic="boundary")
        self.con.execute("UPDATE bugs SET status='INVALID' WHERE symptom='label typo'")  # never a pattern
        out = memory.patterns(self.con)
        self.assertTrue(out.splitlines()[1].startswith("- silent-error-as-empty (error-handling): 3 bugs on 3 pages"), out)
        self.assertNotIn("boundary", out)  # one page only once the INVALID one is out
        self.assertEqual(json.loads(qa_mem("patterns", "--json")[1])[0]["pages"], 3)
        self.assertIn("RECURRING APP PATTERNS", memory.card(self.con, "/schedules") + memory.card(self.con, "/zones"))
        self.assertIn("silent-error-as-empty", memory.ticket_context(self.con, "Schedules list error states")["patterns"][0])
        row = self.con.execute("SELECT heuristic, pattern FROM bugs WHERE page='/incidents'").fetchone()
        self.assertEqual(tuple(row), ("error-handling", "silent-error-as-empty"))


class BugMatch(MemBase):
    """`qa-mem bug-match <route> <symptom> [--endpoint]`: a likely duplicate before a new draft is written."""

    def setUp(self):
        super().setUp()
        with self.con:
            for page in ("/zones/new", "/zones/:id"):
                self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (page, "api", "save", "POST /api/zones", "a.js:1", "/repo", "frontend", core.now(), "a.js"))

    def bug(self, page, title, symptom, status="OPEN", **extra):
        memory.ingest(self.con, {"run_id": f"r-{symptom}", "build": "1", "pages": [{"route": page}],
                                 "bugs": [{"route": page, "title": title, "symptom": symptom, "severity": "HIGH", **extra}]})
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
        code, cli_out, _ = qa_mem("bug-match", "/zones/:id", "form saves speed limit above 130 (131)", "--endpoint", "POST /api/zones")
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


class History(MemBase):
    """Bug build lifecycle, run counters, `qa-mem history` (build over build, a page's timeline) and `qa-mem hotspots`."""

    def go(self, build, ts, cases=(), bugs=(), route="/form", status="COMPLETED"):
        memory.ingest(self.con, {"run_id": f"r-{route}-{ts}", "mode": "ticket", "target": route, "build": build,
                                 "status": status, "summary": "s", "pages": [{"route": route}], "cases": list(cases),
                                 "bugs": list(bugs)}, ts)

    def test_a_bug_knows_its_builds_and_regressions_and_runs_their_counts(self):
        bug = {"route": "/form", "title": "t", "symptom": "s", "severity": "HIGH"}
        self.go("1.0", "2026-01-01T00:00:00Z", [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "FAIL"},
                                                {"id": "TC-3", "status": "BLOCKED"}], [bug])
        h = core.bug_hash("/form", "s")
        with mock.patch.object(core, "now", return_value="2026-01-02T00:00:00Z"):
            cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        self.go("1.1", "2026-01-03T00:00:00Z", bugs=[{**bug, "hash": h}])
        row = self.con.execute("SELECT first_build, last_build, fixed_build, regressed_count, status FROM bugs").fetchone()
        self.assertEqual(tuple(row), ("1.0", "1.1", "1.0", 1, "REGRESSED"))
        counts = self.con.execute("SELECT cases_pass, cases_fail, cases_blocked, bugs_found FROM runs WHERE build='1.0'").fetchone()
        self.assertEqual(tuple(counts), (1, 1, 1, 1))

    def test_history_by_build_and_by_page_and_hotspots(self):
        self.go("1.0", "2026-01-01T00:00:00Z", [{"id": "TC-1", "status": "PASS"}],
                [{"route": "/form", "title": "a", "symptom": "a", "severity": "HIGH"}])
        self.go("1.1", "2026-01-05T00:00:00Z", [{"id": "TC-1", "status": "FAIL"}],
                [{"route": "/form", "title": "b", "symptom": "b", "severity": "LOW"}])
        self.go("1.1", "2026-01-05T01:00:00Z", route="/list")
        out = memory.history(self.con)
        lines = [x for x in out.splitlines() if x.startswith("- build")]
        self.assertTrue(lines[0].startswith("- build 1.1: 2 runs on 2 pages; cases 0 pass / 1 fail / 0 blocked; 1 new bug"), out)
        self.assertTrue(lines[1].startswith("- build 1.0: 1 run on 1 page"), out)
        page = memory.history(self.con, "/form")
        self.assertLess(page.index("build 1.1"), page.index("build 1.0"))  # newest first
        self.assertEqual(json.loads(qa_mem("history", "--json")[1])["builds"][0]["build"], "1.1")
        hot = memory.hotspots(self.con)
        self.assertTrue(hot.splitlines()[1].startswith("- /form: 4"), hot)  # HIGH 3 + LOW 1
        self.assertEqual(json.loads(qa_mem("hotspots", "--json")[1])[0]["page"], "/form")


class SignalBaseline(MemBase):
    """Unexplained browser signals remembered across runs: NEW ones first, recurring ones on the card, noise ignored."""

    def explore(self, name, run_id, ts):
        d = Path(self.tmp.name) / "proj/qa-out" / name / ".data"
        d.mkdir(parents=True)
        (d / "plan.json").write_text(json.dumps({"kind": "explore", "title": "/form", "tickets": [], "cases": []}))
        (d / "requests-form.log").write_text("4. [POST] http://localhost:8765/api/zones/7 => [500] Error\n")
        (d / "console-form.log").write_text("[ERROR] Zone 12 failed to load\n")
        f = d / "run-form.json"
        f.write_text(json.dumps({**run(), "run_id": run_id, "target": "/form"}))
        os.utime(f, (ts, ts))
        memory.ingest_pending(d.parent.parent)
        return d.parent

    def test_signals_are_new_then_recurring_and_can_be_ignored(self):
        self.config(allowedHosts=["localhost"])
        first = self.explore("r1", "r1", time.time() - 3600)
        from qa_sdet import report
        self.assertIn("[NEW] POST /api/zones/7 → 500", report.render_report(first))
        second = self.explore("r2", "r2", time.time())
        r = report.render_report(second)
        self.assertNotIn("[NEW]", r)
        self.assertIn("POST /api/zones/7 → 500 (seen in 2 runs)", r)
        card = memory.card(self.con, "/form")
        self.assertIn("RECURRING UNEXPLAINED SIGNALS", card)
        self.assertIn("POST /api/zones/:id 500", card)
        self.assertIn("console: Zone N failed to load", card)
        sid = self.con.execute("SELECT id FROM signals WHERE kind='request'").fetchone()[0]
        self.assertEqual(qa_mem("signal-ignore", str(sid))[0], 0)
        self.assertNotIn("POST /api/zones/7", report.render_report(second))  # the user called it noise
        self.assertIn("IGNORED", qa_mem("signals", "/form", "--status", "ALL")[1])
        self.assertEqual(qa_mem("signal-unignore", str(sid))[0], 0)
        self.assertIn("POST /api/zones/7", report.render_report(second))


class States(MemBase):
    """In-page states (tabs, dialogs, query views) and page-to-page transitions, recorded by explorers as they go."""

    def visit(self, build, ts, states=(), transitions=()):
        memory.ingest(self.con, {"run_id": f"r-{ts}", "target": "/incidents", "build": build,
                                 "pages": [{"route": "/incidents", "states": list(states), "transitions": list(transitions)}]}, ts)

    def test_unvisited_states_leads_and_the_entry_of_a_detail_page(self):
        tab = lambda name, status: {"key": f"tab: {name}", "kind": "tab", "trigger": f"getByRole('tab', {{ name: '{name}' }})",
                                    "status": status}
        self.visit("1.0", "2026-01-01T00:00:00Z", [tab("Active", "visited"), tab("Scheduled", "unvisited"),
                                                   {"key": "dialog: Filter", "kind": "dialog", "trigger": "getByRole('button', { name: 'Filter' })"}],
                   [{"to": "/incidents/4521", "trigger": "row click", "sample_path": "/incidents/4521?token=abc123secretvalue",
                     "kind": "detail-row"}])
        card = memory.card(self.con, "/incidents")
        line = next(x for x in card.splitlines() if x.startswith("UNVISITED STATES"))
        self.assertIn("tab: Scheduled (getByRole('tab', { name: 'Scheduled' }))", line)  # with the trigger that opens it
        self.assertIn("dialog: Filter (getByRole('button', { name: 'Filter' }))", line)
        self.assertNotIn("tab: Active", line)
        self.assertIn("/incidents/:id (row click)", next(x for x in card.splitlines() if x.startswith("LEADS TO")))
        detail = memory.card(self.con, "/incidents/:id")
        self.assertIn("ENTRY: from /incidents (row click; e.g. /incidents/4521)", detail)
        self.assertNotIn("abc123secretvalue", detail)
        data = json.loads(memory.gaps(self.con, as_json=True))
        listing = next(p for p in data["pages"] if p["route"] == "/incidents")
        self.assertEqual(listing["unvisited_states"], 2)
        self.assertIn("2 unvisited states", listing["risk_reasons"])
        self.assertIn("2 unvisited states", memory.gaps(self.con))
        page = next(p for p in data["pages"] if p["route"] == "/incidents/:id")
        self.assertEqual((page["entry_route"], page["entry_trigger"]), ("/incidents", "row click"))
        self.assertNotIn("/incidents/:id", data["needs_id"])  # it can be opened from its list page now

    def test_a_new_build_makes_visited_states_due_again(self):
        self.visit("1.0", "2026-01-01T00:00:00Z", [{"key": "tab: Active", "kind": "tab", "status": "visited"}])
        self.assertNotIn("UNVISITED STATES", memory.card(self.con, "/incidents"))
        self.visit("2.0", "2026-01-02T00:00:00Z")
        self.assertIn("tab: Active", memory.card(self.con, "/incidents"))


class Database(MemBase):
    """core.connect."""

    def test_a_database_from_before_0_4_is_refused_untouched(self):
        for ddl, missing in (("CREATE TABLE bugs(hash TEXT PRIMARY KEY, page TEXT, title TEXT, seen_count INTEGER)", "bugs lacks"),
                             ("CREATE TABLE codemap(page TEXT, kind TEXT, label TEXT, repo TEXT, area TEXT)", "codemap lacks src")):
            old = Path(self.tmp.name) / f"{missing.split()[0]}.db"
            c = sqlite3.connect(old)
            c.execute(ddl)
            c.commit()
            c.close()
            os.environ["QA_MEM_DB"] = str(old)
            with self.assertRaisesRegex(core.BadInput, f"from before qa-sdet 0.4 \\({missing}"):
                core.connect()
            c = sqlite3.connect(old)
            self.assertEqual(len(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()), 1)  # nothing added
            c.close()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["card", "/x"]), 2)
            self.assertIn("from before qa-sdet 0.4", err.getvalue())


class AuditFollowUps(MemBase):
    """Bug state, gaps tiers and upgrades the audit of the 0.4 features found wrong."""

    def test_a_regression_its_run_no_longer_lists_is_undone(self):  # a checkpoint listed it, the final file dropped it
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        memory.ingest(self.con, run(bugs=[bug]), "2026-01-01T00:00:00Z")
        h = core.bug_hash("/form", "s")
        with mock.patch.object(core, "now", return_value="2026-01-02T00:00:00Z"):
            cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        later = {**run("1.1", bugs=[{**bug, "hash": h}]), "run_id": "r-later", "status": "INCOMPLETE"}
        memory.ingest(self.con, later, "2026-01-03T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT status FROM bugs").fetchone()[0], "REGRESSED")
        memory.ingest(self.con, {**later, "status": "COMPLETED", "bugs": []}, "2026-01-03T00:05:00Z")
        self.assertEqual(tuple(self.con.execute("SELECT status, regressed_count FROM bugs").fetchone()), ("FIXED", 0))

    def test_a_page_only_the_crawler_mapped_is_still_never_explored(self):  # --discover / qa-init --crawl test nothing
        self.con.execute("INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
                         ("/zones", "rule", "speed max 130", "", "a.js:1", "/repo", "frontend", core.now(), "a.js"))
        memory.ingest(self.con, {"run_id": "map-1", "mode": "map", "target": "http://qa", "build": "1.0", "status": "COMPLETED",
                                 "pages": [{"route": "/zones"}]}, "2026-01-01T00:00:00Z")
        page = lambda: next(p for p in json.loads(memory.gaps(self.con, as_json=True))["pages"] if p["route"] == "/zones")
        self.assertEqual((page()["never_explored"], page()["tier"]), (True, "new, with forms"))
        memory.ingest(self.con, {**run(), "target": "/zones", "pages": [{"route": "/zones"}]}, "2026-01-02T00:00:00Z")
        self.assertFalse(page()["never_explored"])

    def test_bugs_from_before_build_tracking_are_not_new_on_a_later_build(self):
        memory.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]), "2026-01-01T00:00:00Z")
        self.con.execute("ALTER TABLE bugs DROP COLUMN first_build")  # as a database from before the column
        self.con.commit()
        self.con.close()
        self.con = core.connect()
        memory.ingest(self.con, run("2.0", bugs=[{"route": "/form", "title": "t", "symptom": "s"}]), "2026-01-05T00:00:00Z")
        b2 = next(x for x in json.loads(memory.history(self.con, as_json=True))["builds"] if x["build"] == "2.0")
        self.assertEqual(b2["new_bugs"], 0)  # it was found before 2.0

    def test_a_locked_database_while_keeping_signals_retries_the_whole_file(self):
        import sqlite3
        out = Path(self.tmp.name) / "qa-out"
        f = runfile(out / "a")
        f.write_text(json.dumps(run()))
        (f.parent / "requests-x.log").write_text("1. [POST] http://localhost/api/z => [500] err\n")
        with mock.patch.object(memory, "_store_signals", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(memory.ingest_pending(out), 0)
        self.assertFalse(f.with_name(f.name + ".ingested").exists())  # not marked: the next hook run keeps its signals
        self.assertEqual(memory.ingest_pending(out), 1)

    def test_adding_a_column_twice_is_harmless(self):  # two processes upgrading one database at once
        core._add_column(self.con, "bugs", "x_test_col", "TEXT")
        core._add_column(self.con, "bugs", "x_test_col", "TEXT")
        self.assertIn("x_test_col", {r[1] for r in self.con.execute("PRAGMA table_info(bugs)")})


class IngestPending(MemBase):
    """`qa-mem ingest-pending`: the run files under qa-out/<run>/.data."""

    def test_ingest_pending_is_idempotent_and_records_errors(self):
        out = Path(self.tmp.name) / "qa-out"
        (out / "a").mkdir(parents=True)
        (out / "b").mkdir()
        runfile(out / "a").write_text(json.dumps(run(facts=[{"kind": "note", "text": "hi"}])))
        runfile(out / "b").write_text("{not json")
        self.assertEqual(memory.ingest_pending(out), 1)
        self.assertTrue((out / "a/.data/run-x.json.ingested").exists())
        self.assertTrue((out / "b/.data/run-x.json.error").exists())
        self.assertEqual(memory.ingest_pending(out), 0)

    def test_odd_text_and_paths_are_ingested_not_parked(self):  # a sliced emoji, a BOM, `~user/` in draft_path
        out = Path(self.tmp.name) / "qa-out"
        for name, data in [("s", json.dumps(run(facts=[{"kind": "note", "text": "emoji cut \ud83d here"}]))),
                           ("b", "\ufeff" + json.dumps(run(facts=[{"kind": "note", "text": "Authorization: Bearer abcdefghijklmnop1234"}]))),
                           ("t", json.dumps(run(bugs=[{"route": "/form", "title": "t", "draft_path": "~nosuchuser9/BUG-1.md"}])))]:
            (out / name).mkdir(parents=True)
            runfile(out / name).write_text(data, encoding="utf-8")
        self.assertEqual(memory.ingest_pending(out), 3, list(out.rglob("*.error")))
        self.assertNotIn("abcdefghijklmnop1234", runfile(out / "b").read_text(encoding="utf-8"))  # the file is scrubbed too
        self.assertIn("emoji cut", memory.search(self.con, "emoji"))

    def test_transient_error_retries(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        runfile(out).write_text(json.dumps(run()))
        real = memory.ingest
        memory.ingest = lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        try:
            self.assertEqual(memory.ingest_pending(out.parent), 0)
        finally:
            memory.ingest = real
        self.assertFalse((out / ".data/run-x.json.error").exists())
        self.assertEqual(memory.ingest_pending(out.parent), 1)

    def test_wrongly_typed_fields_are_parked_not_retried(self):
        out = Path(self.tmp.name) / "qa-out"
        for i, bad in enumerate([{"pages": [{"route": ["/a"]}]}, {"mode": {"x": 1}},
                                 {"bugs": [{"route": "/a", "title": "t", "severity": ["HIGH"]}]},
                                 {"status": 10 ** 30, "pages": [{"route": "/a", "facts": [{"text": {"a": 1}}]}]}]):
            d = out / str(i)
            d.mkdir(parents=True)
            runfile(d).write_text(json.dumps({"run_id": f"r{i}", "build": "1", **bad}))
        memory.ingest_pending(out)
        errors = sorted(p.parent.parent.name for p in out.rglob("run-x.json.error"))
        self.assertEqual(errors[:3], ["0", "1", "2"])

    def test_ingest_pending_cli_dedupes_dirs_and_content_hash(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        runfile(out).write_text(json.dumps(run(bugs=[{"route": "/form", "title": "t"}])))
        cli.main(["ingest-pending", str(out.parent), str(out.parent) + "/", ""])
        self.assertEqual(self.con.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        future = time.time() + 3600  # skewed mtime must not cause re-ingest loops
        os.utime(runfile(out), (future, future))
        self.assertEqual(memory.ingest_pending(out.parent), 0)
        self.assertLessEqual(self.con.execute("SELECT max(ts) FROM runs").fetchone()[0], core.now())

    def test_draft_paths_stored_absolute(self):
        proj = Path(self.tmp.name) / "proj"
        out = proj / "qa-out" / "form"
        (out / "bugs").mkdir(parents=True)
        (out / "bugs/BUG-1.md").write_text("x")
        runfile(out).write_text(json.dumps(run(bugs=[
            {"route": "/form", "title": "a", "draft_path": "qa-out/form/bugs/BUG-1.md"},
            {"route": "/form", "title": "b", "draft_path": "bugs/BUG-1.md"}])))
        memory.ingest_pending(proj / "qa-out")
        paths = {r[0] for r in self.con.execute("SELECT draft_path FROM bugs")}
        self.assertEqual(paths, {str((out / "bugs/BUG-1.md").resolve())})

    def test_a_run_file_with_an_old_mtime_is_still_redacted(self):
        out = Path(self.tmp.name) / "qa-out" / "a"
        out.mkdir(parents=True)
        f = runfile(out)
        f.write_text(json.dumps(run(facts=[{"kind": "note", "text": "login sets eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk"}])))
        os.utime(f, (1, 1))
        (out.parent / ".scrubbed").write_text(str(time.time()))  # scrub skips the old file
        self.con.close()
        memory.ingest_pending(out.parent)
        self.con = core.connect()
        self.assertNotIn("eyJhbGci", " ".join(r[0] for r in self.con.execute("SELECT text FROM facts")))

    def test_run_json_in_data_with_drafts_in_bugs(self):
        d = self.rundir(plan=PLAN, runs=[("form", RUN_FORM)])
        (d / "bugs/BUG-1.md").write_text("draft")
        self.assertEqual(memory.ingest_pending(self.proj / "qa-out"), 2)
        self.assertTrue((d / ".data/run-form.json.ingested").exists())
        self.assertTrue((d / ".data/plan.json.ingested").exists())  # only its questions are kept (facts and bugs come from runs)
        con = core.connect()
        try:
            self.assertEqual(con.execute("SELECT draft_path FROM bugs").fetchone()[0], str((d / "bugs/BUG-1.md").resolve()))
        finally:
            con.close()
        self.assertEqual(memory.ingest_pending(self.proj / "qa-out"), 0)

    def test_bad_report_fields_are_parked(self):
        for bad in ({"cases": [{"id": "TC-1", "status": "MAYBE"}]}, {"cases": [{"status": "PASS"}]},
                    {"cases": "TC-1 PASS"}, {"questions": "why?"}, {"not_tested": [1]}, {"notes": ["x"]},
                    {"questions": [{"status": "open"}]}, {"not_tested": [{"category": "safety", "text": 3}]},
                    {"env": "chrome"}, {"env": {"url": ["x"]}},
                    {"bugs": [{"route": "/x", "title": "t", "annotated": "yes"}]}):
            d = self.rundir("bad")
            f = d / ".data/run-x.json"
            f.write_text(json.dumps({**RUN_LIST, "bugs": [], **bad}))
            memory.PARKED.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                memory.ingest_pending(self.proj / "qa-out")
            self.assertTrue(f.with_name("run-x.json.error").exists(), bad)
            f.with_name("run-x.json.error").unlink()
        good = {**RUN_LIST, "cases": [{"id": "TC-1", "status": "not run"}], "env": {"url": "u", "browser": "firefox"},
                # question and not-tested objects are valid too
                "questions": ["a?", {"text": "b?", "status": "resolved", "answer": "yes"}],
                "not_tested": ["c", {"category": "safety", "text": "d"}]}
        f.write_text(json.dumps(good))
        self.assertEqual(memory.ingest_pending(d), 1)  # a run folder finds its qa-out

    def test_deep_json_does_not_stop_other_ingests(self):  # a RecursionError in scrub used to abort the whole hook
        bad = self.rundir("a") / ".data/run-deep.json"
        bad.write_text("[" * 50000 + "]" * 50000)
        good = self.rundir("b", runs=[("ok", RUN_LIST)]) / ".data/run-ok.json"
        memory.PARKED.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(memory.ingest_pending(self.proj / "qa-out"), 1)
        self.assertTrue(good.with_name("run-ok.json.ingested").exists())
        self.assertTrue(bad.with_name("run-deep.json.error").exists())
        self.assertEqual(len(memory.PARKED), 1)

    def test_concurrent_ingest_pending_rereads_under_lock(self):
        import fcntl, hashlib, threading
        out = Path(self.tmp.name) / "qa-out"
        d = out / "2026-11-24-race"
        d.mkdir(parents=True)
        rf = runfile(d)
        rf.write_text(json.dumps(run(facts=[{"kind": "note", "text": "OLD_STALE_FACT"}])))
        db = core.db_path()
        db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        entered_lock = threading.Event()
        real_flock = fcntl.flock
        # Hold the lock so worker B snapshots _pending before lock, while worker A ingests an updated file first.
        lock_fd = open(db.with_suffix(".lock"), "a")
        real_flock(lock_fd, fcntl.LOCK_EX)
        def waiting_flock(fh, op):
            if fh is not lock_fd:
                entered_lock.set()
            return real_flock(fh, op)
        results = []
        with mock.patch.object(fcntl, "flock", side_effect=waiting_flock):
            t = threading.Thread(target=lambda: results.append(memory.ingest_pending(out)))
            t.start()
            self.assertTrue(entered_lock.wait(5))
            # While B is blocked on flock, rewrite the run file and ingest it as A (holding lock_fd already).
            rf.write_text(json.dumps(run(facts=[{"kind": "note", "text": "NEW_FIXED_FACT"}])))
            con_a = core.connect()
            try:
                memory._ingest_files(con_a, memory._pending(out))
            finally:
                con_a.close()
            lock_fd.close()
            t.join(5)
        self.assertEqual(results, [0])  # B re-checked under the lock and did not re-ingest stale pre-lock bytes
        self.con.close()
        self.con = core.connect()
        self.assertEqual([r[0] for r in self.con.execute("SELECT text FROM facts")], ["NEW_FIXED_FACT"])
        self.assertEqual((d / ".data/run-x.json.ingested").read_text().splitlines()[0], hashlib.sha1(rf.read_bytes()).hexdigest())


class InitStatus(MemBase):
    """`qa-mem init-status <init dir>`: which mapper chunks of the newest scan are settled, and which to start again."""

    def test_settled_parked_missing_and_stale_maps(self):
        import hashlib
        d = Path(self.tmp.name) / "qa-out/init"
        d.mkdir(parents=True)
        def scan(name, sid, files):
            (d / f"scan-{name}.json").write_text(json.dumps({"scan_id": sid, "area": name.split("-")[0],
                                                              "repos": [{"repo": "/r", "files": files}]}))
        def cmap(name, sid, sentinel=None, note=""):
            f = d / f"code-map-{name}.json"
            f.write_text(json.dumps({"scan_id": sid, "area": name.split("-")[0], "rows": []}))
            if sentinel:
                (d / f"{f.name}.{sentinel}").write_text(f"{hashlib.sha1(f.read_bytes()).hexdigest()}\n2026-11-25T10:00:00Z {note}\n")
        new = "20261125-100000"
        scan("frontend-1", f"frontend-1-{new}", ["a.jsx"])
        scan("frontend-2", f"frontend-2-{new}", ["b.jsx"])
        scan("frontend-3", f"frontend-3-{new}", ["c.jsx"])
        scan("backend", f"backend-{new}", ["d.js"])
        scan("tests", f"tests-{new}", [])  # nothing to read: settled, no mapper
        scan("old", "old-20261120-090000", ["x.js"])  # a scan file left from an earlier scan
        cmap("frontend-1", f"frontend-1-{new}", "ingested")
        (d / "code-map-frontend-1.json.ingested").write_text(
            (d / "code-map-frontend-1.json.ingested").read_text().split("\n")[0] + "\nT\ndropped rows: {'kind': 2}\n")
        cmap("frontend-2", f"frontend-2-{new}", "error", "ValueError: row 3 has no label")
        cmap("backend", "backend-20261120-090000", "ingested")  # the mapper didn't write this scan's map
        code, out, _ = qa_mem("init-status", str(d))
        st = json.loads(out)
        self.assertEqual((code, st["total"], st["ingested"]), (0, 4, 1))
        self.assertEqual(st["dropped_rows"], {str(d / "code-map-frontend-1.json"): "{'kind': 2}"})
        retry = {Path(r["out"]).name: r for r in st["retry"]}
        self.assertEqual(set(retry), {"code-map-frontend-2.json", "code-map-frontend-3.json", "code-map-backend.json"})
        self.assertIn("row 3 has no label", retry["code-map-frontend-2.json"]["reason"])
        self.assertIn("no code map", retry["code-map-frontend-3.json"]["reason"])
        self.assertIn("older scan", retry["code-map-backend.json"]["reason"])
        self.assertEqual((retry["code-map-backend.json"]["area"], retry["code-map-backend.json"]["scan"]),
                         ("backend", str(d / "scan-backend.json")))


if __name__ == "__main__":
    unittest.main()
