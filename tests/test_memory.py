"""Memory readers (qa_sdet.memory, core): search, the user's questions and facts, patterns, the signal baseline, the DB
itself, and what an audit of the 0.4 features found wrong."""

import contextlib
import io
import json
import os
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from support import MemBase, core, run, runfile
from support import cli as qa_mem

from qa_sdet import cards, cli, gaps, ingest, memory


class Search(MemBase):
    """`qa-mem search`."""

    def test_text_capped_and_fts_search(self):
        ingest.ingest(self.con, run(facts=[{"kind": "rule", "text": "closure schedule " + "x" * 500}]))
        t = self.con.execute("SELECT text FROM facts").fetchone()[0]
        self.assertEqual(len(t), core.MAX_TEXT)
        self.assertIn("closure", memory.search(self.con, "closure"))
        self.assertFalse(memory.search(self.con, "closure").endswith("*"))
        ingest.ingest(self.con, run("2.0", facts=[{"kind": "rule", "text": "other"}]))  # new build stales old facts
        self.assertTrue(memory.search(self.con, "closure").endswith(" *"))

    def test_search_with_broken_fts_syntax_falls_back_to_words(self):
        ingest.ingest(self.con, run(facts=[{"kind": "rule", "text": "I-5 N closure: lane 2 (a-b) 50% done"}]))
        for q in ["closure AND", "NEAR(closure", "a-b", "I-5", "closure lane"]:
            self.assertIn("closure", memory.search(self.con, q), q)
        self.assertIn("no facts match", memory.search(self.con, "*"))

    def test_operators_in_the_query_are_words(self):
        ingest.ingest(
            self.con,
            run(
                "1.43.0",
                facts=[{"kind": "rule", "text": "speed max 130"}, {"kind": "rule", "text": "closure AND detour shown"}],
            ),
        )
        self.assertNotIn("max 130", memory.search(self.con, "speed -130"))
        self.assertIn("detour", memory.search(self.con, "closure NEAR detour"))
        self.assertIn("max 130", memory.search(self.con, 'speed "130'))

    def test_symbols_in_the_query(self):
        ingest.ingest(
            self.con,
            run(
                facts=[
                    {"kind": "api", "text": "GET /api/zones returns 500 on empty filter"},
                    {"kind": "note", "text": "speed chart loads slowly"},
                ]
            ),
        )
        self.assertIn("returns 500", memory.search(self.con, "/api/zones"))
        self.assertIn("no facts match", memory.search(self.con, "speed-limit"))
        self.assertIn("speed chart", memory.search(self.con, "speed -/api/zones"))


class Retrieval(MemBase):
    """Stemmed search over facts and bugs, a relaxed match when nothing matches all words, and card ranking/focus."""

    def test_word_forms_match_and_bugs_are_searched(self):
        ingest.ingest(
            self.con,
            run(
                facts=[{"kind": "rule", "text": "Corridor dates display in UTC"}],
                bugs=[{"route": "/form", "title": "Zone validation skipped", "symptom": "zones saved unvalidated"}],
            ),
        )
        self.assertIn("Corridor dates", memory.search(self.con, "date"))  # 'date' finds 'dates'
        out = memory.search(self.con, "zone validate")
        self.assertIn("[bug:OPEN]", out)
        self.assertIn("Zone validation skipped", out)
        h = core.bug_hash("/form", "zones saved unvalidated")
        cli.run_cmd(self.con, "bug-status", [h, "FILED", "PROJ-9"])
        self.assertIn("[bug:FILED PROJ-9]", memory.search(self.con, "proj-9"))  # the update reached the index

    def test_no_match_of_all_words_relaxes_to_two_of_them_and_says_so(self):
        ingest.ingest(
            self.con, run(facts=[{"kind": "rule", "text": "Corridor dates display in UTC instead of local time"}])
        )
        out = memory.search(self.con, "date filter timezone UTC")
        self.assertIn("Corridor dates", out)
        self.assertIn("relaxed", out)
        self.assertIn("no facts match", memory.search(self.con, "date filter"))  # two words: no relaxing
        self.assertIn("no facts match", memory.search(self.con, "date filter UTC -corridor"))  # exclusions still hold

    def test_an_old_database_index_is_upgraded_in_place(self):
        ingest.ingest(self.con, run(facts=[{"kind": "rule", "text": "Corridor dates display in UTC"}]))
        self.con.executescript(
            "DROP TRIGGER facts_ai; DROP TRIGGER facts_ad; DROP TABLE facts_fts; "
            "CREATE VIRTUAL TABLE facts_fts USING fts5(text, content='facts', content_rowid='id'); "
            "INSERT INTO facts_fts(facts_fts) VALUES('rebuild'); DELETE FROM meta WHERE key='fts';"
        )
        self.con.close()
        self.con = core.connect()
        self.assertIn("porter", self.con.execute("SELECT sql FROM sqlite_master WHERE name='facts_fts'").fetchone()[0])
        self.assertIn("Corridor dates", memory.search(self.con, "date"))

    def test_a_precondition_outranks_newer_notes(self):  # what a page needs before it can be judged
        need = "Edit is off for records another system owns: a limit of the record, not a bug"
        ingest.ingest(self.con, run(facts=[{"kind": "precondition", "text": need}]), "2026-01-01T00:00:00Z")
        ingest.ingest(
            self.con,
            run(facts=[{"kind": "note", "text": f"layout note {i} {'x' * i}"} for i in range(20)]),
            "2026-01-02T00:00:00Z",
        )
        self.assertIn(f"- [precondition] {need}", cards.card(self.con, "/form"))

    def test_rules_outrank_newer_notes_and_a_focus_comes_first(self):
        ingest.ingest(self.con, run(facts=[{"kind": "rule", "text": "date filter uses UTC"}]), "2026-01-01T00:00:00Z")
        ingest.ingest(
            self.con,
            run(facts=[{"kind": "note", "text": f"layout note {i} {'x' * i}"} for i in range(20)]),
            "2026-01-02T00:00:00Z",
        )
        facts = lambda c: c.split("FACTS", 1)[1].split("\n")[1:]
        self.assertTrue(
            any("date filter uses UTC" in x for x in facts(cards.card(self.con, "/form")))
        )  # not evicted by notes
        ingest.ingest(
            self.con,
            run(facts=[{"kind": "rule", "text": f"rule {i} {'y' * i}"} for i in range(20)]),
            "2026-01-03T00:00:00Z",
        )
        self.assertIn("date filter uses UTC", facts(cards.card(self.con, "/form", focus="date filters"))[0])
        code, out, _ = qa_mem("card", "/form", "--focus", "date filters")
        self.assertEqual(code, 0)
        self.assertIn("date filter uses UTC", out)


class Curation(MemBase):
    """Questions answered once stay answered (DECISIONS); the user's own facts (remember, pinned) and clean-up (forget)."""

    def test_an_answered_question_becomes_a_decision_on_the_card(self):
        q = "Is 130 inclusive?"
        ingest.ingest(self.con, {**run(), "questions": [q, "Units?"]}, "2026-01-01T00:00:00Z")
        c = cards.card(self.con, "/form")
        self.assertIn("OPEN QUESTIONS", c)
        self.assertIn(q, c.split("OPEN QUESTIONS")[1])
        ingest.ingest(
            self.con,
            {**run(), "questions": [{"text": q, "status": "resolved", "answer": "Yes, 0..130"}]},
            "2026-01-02T00:00:00Z",
        )
        ingest.ingest(self.con, {**run(), "questions": [q]}, "2026-01-03T00:00:00Z")  # asked again: stays answered
        c = cards.card(self.con, "/form")
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
        (d / "plan.json").write_text(
            json.dumps(
                {
                    "kind": "ticket",
                    "tickets": [{"key": "P-1"}],
                    "cases": [],
                    "questions": [
                        {"text": "Q1 [BLOCKING] Is 0 valid?", "status": "resolved", "answer": "No", "ticket": "PROJ-1"}
                    ],
                }
            )
        )
        ingest.ingest_pending(d.parent.parent)
        self.assertEqual(
            tuple(self.con.execute("SELECT page, ticket, status, answer FROM questions").fetchone()),
            ("*", "PROJ-1", "resolved", "No"),
        )
        self.assertIn(
            "Is 0 valid? → No (PROJ-1)", memory.ticket_context(self.con, "Speed rules for PROJ-1")["decisions"][0]
        )

    def test_remembered_facts_are_pinned_and_forget_cleans_up(self):
        ingest.ingest(self.con, run("1.0"))  # the page is on build 1.0
        code, out, _ = qa_mem(
            "remember", "/form", "flow", "Create a corridor first. Authorization: Bearer abcdefghijklmnopqrst"
        )
        self.assertEqual(code, 0)
        qa_mem("remember", "/form", "note", "Header is blue", "--build-scoped")
        self.assertEqual(qa_mem("remember", "/form", "wish", "x")[0], 2)
        ingest.ingest(self.con, run("2.0", facts=[{"kind": "note", "text": f"n{i} {'z' * i}"} for i in range(110)]))
        c = cards.card(self.con, "/form")
        pinned = next(x for x in c.splitlines() if "Create a corridor first" in x)
        self.assertIn("[pinned]", pinned)
        self.assertFalse(pinned.endswith("*"))  # a new build doesn't make it stale; the 100-fact cap kept it
        self.assertNotIn("abcdefghijklmnopqrst", c)
        self.assertIsNone(self.con.execute("SELECT 1 FROM facts WHERE text='Header is blue' AND stale=0").fetchone())
        fid = self.con.execute("SELECT id FROM facts WHERE text LIKE 'Create a corridor%'").fetchone()[0]
        self.assertIn(f"#{fid}", qa_mem("card", "/form", "--ids")[1])
        self.assertEqual(qa_mem("forget", "fact", str(fid))[0], 0)
        self.assertNotIn("Create a corridor", cards.card(self.con, "/form"))
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]))
        code, out, _ = qa_mem("forget", "page", "/form")
        self.assertEqual(code, 1)
        self.assertIn("open bug", out)
        self.assertEqual(qa_mem("forget", "page", "/form", "--force")[0], 0)
        self.assertIsNone(self.con.execute("SELECT 1 FROM pages WHERE route='/form'").fetchone())
        self.assertEqual(self.con.execute("SELECT count(*) FROM facts WHERE page='/form'").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM bugs").fetchone()[0], 1)  # bug history stays

    def test_forget_page_also_drops_its_states_transitions_signals_and_questions(self):
        ts = core.now()
        ingest.ingest(self.con, run())
        ingest.ingest(self.con, {"run_id": "o", "build": "1", "pages": [{"route": "/other"}]})
        with self.con:
            for page in ("/form", "/other"):
                self.con.execute(
                    "INSERT INTO page_states VALUES(?,?,?,?,?,?,?)",
                    (page, "tab: A", "tab", "click A", "unvisited", "1", ts),
                )
                self.con.execute("INSERT INTO signals(page,kind,sig_key) VALUES(?,?,?)", (page, "request", "GET /x"))
                self.con.execute("INSERT INTO questions(page,text) VALUES(?,?)", (page, "Is 0 valid?"))
            self.con.execute("INSERT INTO transitions VALUES('/form','/other','link','/other','link','1',?)", (ts,))
            self.con.execute("INSERT INTO transitions VALUES('/other','/form','link','/form','link','1',?)", (ts,))
            self.con.execute("INSERT INTO transitions VALUES('/other','/third','link','/third','link','1',?)", (ts,))
        self.assertEqual(memory.forget_page(self.con, "/form")[0], True)
        for table, col in (("page_states", "page"), ("signals", "page"), ("questions", "page")):
            self.assertEqual([r[0] for r in self.con.execute(f"SELECT {col} FROM {table}")], ["/other"], table)
        self.assertEqual(
            [tuple(r) for r in self.con.execute("SELECT from_route, to_route FROM transitions")], [("/other", "/third")]
        )


class Patterns(MemBase):
    """`qa-mem patterns`: how this app breaks again and again, from the heuristic and pattern each bug was recorded with."""

    def bug(self, page, symptom, **extra):
        ingest.ingest(
            self.con,
            {
                "run_id": f"r-{page}-{symptom}",
                "target": page,
                "build": "1",
                "pages": [{"route": page}],
                "bugs": [{"route": page, "title": symptom, "symptom": symptom, **extra}],
            },
        )

    def test_patterns_across_pages_rank_and_show_on_cards_and_in_prep(self):
        for page in ("/incidents", "/zones", "/closures"):
            self.bug(
                page, f"{page} shows 0 items on a 500", heuristic="error-handling", pattern="Silent error as EMPTY!"
            )
        self.bug("/zones", "speed 131 saved", heuristic="boundary")
        self.bug("/form", "label typo", heuristic="boundary")
        self.con.execute("UPDATE bugs SET status='INVALID' WHERE symptom='label typo'")  # never a pattern
        out = memory.patterns(self.con)
        self.assertTrue(
            out.splitlines()[1].startswith("- silent-error-as-empty (error-handling): 3 bugs on 3 pages"), out
        )
        self.assertNotIn("boundary", out)  # one page only once the INVALID one is out
        self.assertEqual(json.loads(qa_mem("patterns", "--json")[1])[0]["pages"], 3)
        self.assertIn("RECURRING APP PATTERNS", cards.card(self.con, "/schedules") + cards.card(self.con, "/zones"))
        self.assertIn(
            "silent-error-as-empty", memory.ticket_context(self.con, "Schedules list error states")["patterns"][0]
        )
        row = self.con.execute("SELECT heuristic, pattern FROM bugs WHERE page='/incidents'").fetchone()
        self.assertEqual(tuple(row), ("error-handling", "silent-error-as-empty"))


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
        ingest.ingest_pending(d.parent.parent)
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
        card = cards.card(self.con, "/form")
        self.assertIn("RECURRING UNEXPLAINED SIGNALS", card)
        self.assertIn("POST /api/zones/:id 500", card)
        self.assertIn("console: Zone N failed to load", card)
        sid = self.con.execute("SELECT id FROM signals WHERE kind='request'").fetchone()[0]
        self.assertEqual(qa_mem("signal-ignore", str(sid))[0], 0)
        self.assertNotIn("POST /api/zones/7", report.render_report(second))  # the user called it noise
        self.assertIn("IGNORED", qa_mem("signals", "/form", "--status", "ALL")[1])
        self.assertEqual(qa_mem("signal-unignore", str(sid))[0], 0)
        self.assertIn("POST /api/zones/7", report.render_report(second))


class Database(MemBase):
    """core.connect."""

    def test_a_database_from_before_0_4_is_refused_untouched(self):
        for ddl, missing in (
            ("CREATE TABLE bugs(hash TEXT PRIMARY KEY, page TEXT, title TEXT, seen_count INTEGER)", "bugs lacks"),
            ("CREATE TABLE codemap(page TEXT, kind TEXT, label TEXT, repo TEXT, area TEXT)", "codemap lacks src"),
        ):
            old = Path(self.tmp.name) / f"{missing.split()[0]}.db"
            c = sqlite3.connect(old)
            c.execute(ddl)
            c.commit()
            c.close()
            os.environ["QA_MEM_DB"] = str(old)
            with self.assertRaisesRegex(core.BadInput, f"from before qa-sdet 0.4 \\({missing}"):
                core.connect()
            c = sqlite3.connect(old)
            self.assertEqual(
                len(c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()), 1
            )  # nothing added
            c.close()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["card", "/x"]), 2)
            self.assertIn("from before qa-sdet 0.4", err.getvalue())


class AuditFollowUps(MemBase):
    """Bug state, gaps tiers and upgrades the audit of the 0.4 features found wrong."""

    def test_a_regression_its_run_no_longer_lists_is_undone(self):  # a checkpoint listed it, the final file dropped it
        bug = {"route": "/form", "title": "t", "symptom": "s"}
        ingest.ingest(self.con, run(bugs=[bug]), "2026-01-01T00:00:00Z")
        h = core.bug_hash("/form", "s")
        with mock.patch.object(core, "now", return_value="2026-01-02T00:00:00Z"):
            cli.run_cmd(self.con, "bug-status", [h, "FIXED"])
        later = {**run("1.1", bugs=[{**bug, "hash": h}]), "run_id": "r-later", "status": "INCOMPLETE"}
        ingest.ingest(self.con, later, "2026-01-03T00:00:00Z")
        self.assertEqual(self.con.execute("SELECT status FROM bugs").fetchone()[0], "REGRESSED")
        ingest.ingest(self.con, {**later, "status": "COMPLETED", "bugs": []}, "2026-01-03T00:05:00Z")
        self.assertEqual(tuple(self.con.execute("SELECT status, regressed_count FROM bugs").fetchone()), ("FIXED", 0))

    def test_a_page_only_the_crawler_mapped_is_still_never_explored(self):  # --discover / qa-init --crawl test nothing
        self.con.execute(
            "INSERT INTO codemap(page,kind,label,value,file,repo,area,ts,src) VALUES(?,?,?,?,?,?,?,?,?)",
            ("/zones", "rule", "speed max 130", "", "a.js:1", "/repo", "frontend", core.now(), "a.js"),
        )
        ingest.ingest(
            self.con,
            {
                "run_id": "map-1",
                "mode": "map",
                "target": "http://qa",
                "build": "1.0",
                "status": "COMPLETED",
                "pages": [{"route": "/zones"}],
            },
            "2026-01-01T00:00:00Z",
        )
        page = lambda: next(p for p in json.loads(gaps.gaps(self.con, as_json=True))["pages"] if p["route"] == "/zones")
        self.assertEqual((page()["never_explored"], page()["tier"]), (True, "new, with forms"))
        ingest.ingest(self.con, {**run(), "target": "/zones", "pages": [{"route": "/zones"}]}, "2026-01-02T00:00:00Z")
        self.assertFalse(page()["never_explored"])

    def test_bugs_from_before_build_tracking_are_not_new_on_a_later_build(self):
        ingest.ingest(self.con, run(bugs=[{"route": "/form", "title": "t", "symptom": "s"}]), "2026-01-01T00:00:00Z")
        self.con.execute("ALTER TABLE bugs DROP COLUMN first_build")  # as a database from before the column
        self.con.commit()
        self.con.close()
        self.con = core.connect()
        ingest.ingest(
            self.con, run("2.0", bugs=[{"route": "/form", "title": "t", "symptom": "s"}]), "2026-01-05T00:00:00Z"
        )
        b2 = next(x for x in json.loads(gaps.history(self.con, as_json=True))["builds"] if x["build"] == "2.0")
        self.assertEqual(b2["new_bugs"], 0)  # it was found before 2.0

    def test_a_locked_database_while_keeping_signals_retries_the_whole_file(self):
        import sqlite3

        out = Path(self.tmp.name) / "qa-out"
        f = runfile(out / "a")
        f.write_text(json.dumps(run()))
        (f.parent / "requests-x.log").write_text("1. [POST] http://localhost/api/z => [500] err\n")
        with mock.patch.object(ingest, "_store_signals", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(ingest.ingest_pending(out), 0)
        self.assertFalse(f.with_name(f.name + ".ingested").exists())  # not marked: the next hook run keeps its signals
        self.assertEqual(ingest.ingest_pending(out), 1)

    def test_adding_a_column_twice_is_harmless(self):  # two processes upgrading one database at once
        core._add_column(self.con, "bugs", "x_test_col", "TEXT")
        core._add_column(self.con, "bugs", "x_test_col", "TEXT")
        self.assertIn("x_test_col", {r[1] for r in self.con.execute("PRAGMA table_info(bugs)")})


if __name__ == "__main__":
    unittest.main()
