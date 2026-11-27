"""REPORT.md and the run index (qa_sdet.report): what the report says, its layout and its escaping."""

import fcntl
import json
import os
import re
import threading
import unittest
from unittest import mock

from support import PLAN, PLAN1, PLAN1_ANSWERED, RUN1, RUN_FORM, RUN_LIST, Base, Rendering, cli, spacing_problems

from qa_sdet import cards, core, inbox, ingest, report


class Report(Base):
    """REPORT.md and the index as a whole: report kinds, odd input and the run index."""

    def test_ticket_report_two_tickets_two_routes(self):
        d = self.rundir(plan=PLAN, runs=[("form", RUN_FORM), ("list", RUN_LIST)])
        (d / "bugs/BUG-1.md").write_text("x")
        (d / "bugs/BUG-2.md").write_text("x")
        (d / "evidence/BUG-1.png").write_bytes(b"png")
        code, out, _ = cli("report", str(d))
        self.assertEqual((code, out.strip()), (0, str(d / "REPORT.md")))
        r = (d / "REPORT.md").read_text()
        blocks = r.split("\n\n")
        self.assertEqual(blocks[:2], ["# QA report: PROJ-1, PROJ-2", "ticket · 2026-11-23 · build 1.42"])
        self.assertEqual(  # one line per ticket: its verdict and why, once
            blocks[2],
            "- PROJ-1: ❌ FAIL — TC-2 failed (AC-2); BUG-1 HIGH\n- PROJ-2: ⚠️ PASS WITH NOTES — TC-4 open question; BUG-2 LOW",
        )
        self.assertIn(
            "## PROJ-1: Speed limit form\n\n❌ FAIL · build 1.42 · 1 of 2 cases passed · 1 bug\n\n### Results", r
        )
        self.assertIn("## PROJ-2: Incident list\n\n⚠️ PASS WITH NOTES · build 1.42 · 1 of 2 cases passed · 1 bug", r)
        self.assertIn("#### AC-1 Saves 0-130: PASS\n\n- **TC-1 Save 130: PASS.** Saved\n", r)
        self.assertIn(
            "#### AC-2 Rejects 131: FAIL\n\n- **TC-2 Reject 131: FAIL ([BUG-1](bugs/BUG-1.md)).** 131 saved\n", r
        )
        self.assertIn("#### AC-1 Sorted by date: QUESTION\n\n- **TC-3 Sort: PASS.**", r)
        self.assertIn("- **TC-4 Ties: QUESTION.** Order of ties unspecified\n", r)
        self.assertIn("- **Scope:** speed field\n- **Risks:**\n  - rounding\n- **Out of scope:**\n  - mobile", r)
        self.assertIn("  Page: /form · priority P1\n\n  1. Open form\n  2. Type 130 \\| save\n\n  Expected: Saved", r)
        self.assertIn(  # the case that failed on it, and its AC
            "#### BUG-1 Speed accepts 131 (HIGH, CONFIRMED)\n\n- Page /form · found by TC-2 (AC-2)\n\n"
            "- [draft](bugs/BUG-1.md) · [image](evidence/BUG-1.png) · ⚠",
            r,
        )
        self.assertIn(
            "#### BUG-2 Header misaligned (LOW, CONFIRMED)\n\n- Page /list\n\n- [draft](bugs/BUG-2.md) · ⚠ no repro spec",
            r,
        )
        self.assertIn(
            "3. Answer: Is 0 a valid speed? _(from /form)_\n\n4. Answer the question TC-4 raised: see Results.", r
        )
        self.assertIn("<details><summary>Other (1)</summary>\n\n- Keyboard only: out of time", r)
        self.assertIn(
            "<details><summary>/form (run-form.json: COMPLETED, build 1.42) — boundary ❌, happy-path ✅</summary>\n\n"
            "Explored the speed field.",
            r,
        )
        self.assertIn("<details><summary>/list (run-list.json: COMPLETED, build 1.42)</summary>", r)
        order = [
            r.index(h)
            for h in (
                "## What to do",
                "## PROJ-1",
                "### Bugs",
                "## PROJ-2",
                "## Not tested",
                "## Exploration notes",
            )
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(
            sorted(p.name for p in d.glob("jira-comment*")), ["jira-comment-PROJ-1.txt", "jira-comment-PROJ-2.txt"]
        )

    def test_a_known_bug_resighted_as_run_json_says_counts_like_a_new_one(self):
        """run-json.md: a re-sighting carries the hash and severity its card line shows, classification CONFIRMED. Rated
        HIGH, it fails the ticket (README FAIL rule), as a new HIGH bug would."""
        self.rundir(
            name="2026-11-22-PROJ-0",
            runs=[
                (
                    "form",
                    {
                        **RUN1,
                        "run_id": "r-0",
                        "cases": [],
                        "bugs": [
                            {
                                "route": "/form",
                                "title": "Speed accepts 131",
                                "symptom": "131 saved",
                                "severity": "HIGH",
                                "classification": "CONFIRMED",
                            }
                        ],
                    },
                )
            ],
        )
        ingest.ingest_pending(self.proj / "qa-out")
        con = core.connect()
        try:
            line = next(x for x in cards.card(con, "/form").splitlines() if "Speed accepts 131" in x)
        finally:
            con.close()
        h, sev = re.match(r"- ([0-9a-f]{12}) \w+ \[([^\]]+)\]", line).groups()
        resight = {
            "route": "/form",
            "title": "Speed accepts 131",
            "hash": h,
            "severity": sev,
            "classification": "CONFIRMED",
        }
        d = self.rundir(
            name="2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", {**RUN1, "run_id": "r-1", "bugs": [resight]})]
        )
        r = report.render_report(d)
        self.assertIn("\n\n❌ FAIL · build 1.42 · ", r)
        self.assertIn("\n\nknown bug Speed accepts 131 HIGH\n\n", r)  # the reason, under the status line

    def test_nothing_run_yet(self):
        d = self.rundir(plan=PLAN)
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertEqual(r.count("NOT RUN"), 4 + 3)  # every case, and every covered AC
        self.assertIn("- PROJ-1: ⏳ NOT STARTED — testing hasn't started yet\n", r)
        self.assertIn("⏳ NOT STARTED · build not run yet · 0 of 2 cases passed · no bugs", r)
        self.assertEqual(list(d.glob("jira-comment*")), [])  # nothing to say yet: no comment
        for absent in ("# Bugs", "## Exploration notes", "## Questions"):
            self.assertNotIn(absent, r)

    def test_empty_folder_and_broken_files(self):
        d = self.rundir("2026-11-20-x")
        self.assertIn("Nothing recorded yet", report.render_report(d))
        (d / ".data/plan.json").write_text("{nope")
        (d / ".data/run-a.json").write_text("[1]")
        r = report.render_report(d)
        self.assertIn(
            "## Problems reading this folder\n\n- plan.json unreadable (JSONDecodeError)\n- run-a.json is not an object",
            r,
        )
        self.assertEqual(cli("report", str(self.proj))[0], 2)  # not a qa-out/<run> folder

    def test_explore_kind(self):
        run = {**RUN_FORM, "mode": "explore", "cases": [], "bugs": [{**RUN_FORM["bugs"][0], "ticket": None}]}
        d = self.rundir(
            "2026-11-23-form",
            plan={"kind": "explore", "title": "/form", "env": {"url": "http://localhost:8765"}},
            runs=[("form", run)],
        )
        r = report.render_report(d)
        self.assertEqual(
            r.split("\n\n")[:2], ["# QA report: /form", "explore · 2026-11-23 · build 1.42 · /form COMPLETED · 1 bug"]
        )
        self.assertNotIn("# Results", r)  # no ACs, no verdict
        self.assertIn("1. Check the bug: BUG-1 Speed accepts 131 (HIGH)\n", r)  # no draft: nothing to file, no link
        self.assertIn(
            "## Bugs\n\n### BUG-1 Speed accepts 131 (HIGH, CONFIRMED)\n\n- Page /form\n- ⚠ no draft bugs/BUG-1.md", r
        )
        self.assertIn("## Exploration notes", r)
        self.assertNotIn("History", r)

    def test_plan_kind(self):
        d = self.rundir("2026-11-23-plan-PROJ-1", plan={**PLAN, "kind": "plan"})
        r = report.render_report(d)
        self.assertEqual(
            r.split("\n\n")[:2], ["# QA report: PROJ-1, PROJ-2", "plan · 2026-11-23 · not run yet · 4 cases"]
        )
        self.assertIn("- PROJ-1: Speed limit form — 2 AC, 2 cases\n- PROJ-2: Incident list — 1 AC, 2 cases\n", r)
        self.assertIn("## PROJ-1: Speed limit form\n\n### Cases\n\n", r)
        self.assertIn("#### AC-1 Saves 0-130\n\n- **TC-1 Save 130.**\n\n  <details><summary>Steps</summary>", r)
        for absent in ("PASS", "NOT RUN", "What to do", "jira-comment"):
            self.assertNotIn(absent, r)
        one = report.render_report(self.rundir("2026-11-23-plan-1", plan={**PLAN1, "kind": "plan", "questions": []}))
        self.assertEqual(
            one.split("\n\n")[:3],
            ["# PROJ-1: Speed limit form", "plan · 2026-11-23 · not run yet · 2 cases", "## Cases"],
        )

    def test_plan_questions_render(self):
        q = {**PLAN, "kind": "plan", "questions": ["Q1 [BLOCKING] Is 0 a valid speed?", "Q2 [LOW] Units?"]}
        r = report.render_report(self.rundir("2026-11-23-plan-PROJ-1", plan=q))
        self.assertIn("## Questions\n\n- Q1 [BLOCKING] Is 0 a valid speed?\n- Q2 [LOW] Units?", r)
        d = self.rundir(plan={**q, "kind": "ticket"}, runs=[("form", RUN_FORM)])
        # a run's follow-up on plan Q1 read as a second, unnumbered Q1 in one flat list: two labelled lists
        r = report.render_report(d)
        self.assertIn("Answer: Q1 [BLOCKING] Is 0 a valid speed? _(from test plan)_", r)  # a run's follow-up: one list
        self.assertIn("Answer: Q2 [LOW] Units? _(from test plan)_", r)
        self.assertIn("Heuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked", r)

    def test_odd_field_types_never_crash(self):
        t = [{"key": "PROJ-1", "summary": "A", "acs": [{"id": "AC-1", "text": "x"}]}]
        c = [{"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1", "title": "t", "route": "/f", "steps": ["a"]}]
        plans = [
            {"kind": "ticket", "tickets": t, "cases": c, "env": {"url": 5}},
            {"kind": "ticket", "tickets": t, "cases": c, "env": {"url": ["x"], "browser": {"a": 1}}},
            {"kind": "ticket", "tickets": [{"key": ["a"]}, {"key": 12}], "cases": [{"id": ["x"], "ticket": 12}]},
            {"kind": "plan", "created": 2026, "title": ["x"], "tickets": t, "cases": {"a": 1}, "questions": "q"},
            {"kind": ["x"], "tickets": "x", "cases": [{**c[0], "steps": "do | it\nnow", "ac": ["x"]}]},
            {
                "kind": "ticket",
                "tickets": t,
                "cases": [
                    {**c[0], "ticket": ["PROJ-1"]},
                    {**c[0], "ticket": {"k": 1}},
                    {**c[0], "id": "TC-2", "ticket": ["x"]},
                ],
            },
        ]  # TC-1 twice
        runs = [
            {"env": {"url": 7, "browser": 3}, "target": 5, "status": ["x"], "build": {"b": 1}},
            {"bugs": [{"ticket": ["x"], "title": {"a": 1}, "route": "/f", "hash": 5}]},
            {"cases": [{"id": ["x"], "status": "PASS"}, {"id": {"a": 1}, "status": 3}, {"id": "TC-1", "bug": 9}]},
            {"pages": [{"route": "/f", "coverage": [{"heuristic": 5, "result": ["x"]}]}], "notes": ["n"]},
        ]
        for i, plan in enumerate(plans):
            d = self.rundir(f"2026-11-23-fuzz{i}", plan=plan, runs=[(f"r{k}", r) for k, r in enumerate(runs)])
            code, _, err = cli("report", str(d))
            self.assertEqual((code, err), (0, ""), plan)
            self.assertTrue((d / "REPORT.md").read_text().startswith("# "), plan)
            self.assertNotIn("Could not render", (d / "REPORT.md").read_text(), plan)  # rendered, not the fallback
            self.assertEqual(cli("plan-find", str(self.proj), "--run", str(d))[0], 0, plan)  # --resume/--retest state
        self.assertEqual((self.proj / "qa-out/README.md").read_text().count("| 2026-11-23 |"), len(plans))
        self.assertEqual(
            cli("plan-find", str(self.proj), "--keys", "PROJ-1")[0], 0
        )  # prep reads every folder like this
        con = core.connect()
        try:
            self.assertIn("| PROJ-1 |", inbox.inbox(con, self.proj, [{"key": "PROJ-1", "fields": {"summary": "s"}}]))
        finally:
            con.close()

    def test_odd_ac_ids_do_not_lose_the_report(self):
        plan = json.loads(json.dumps(PLAN))
        plan["tickets"][0]["acs"] = [{"id": ["AC-1"], "text": "a list for an id"}, {"id": 2, "text": "a number"}]
        plan["cases"][1]["ac"] = 2
        d = self.rundir(
            plan=plan,
            runs=[
                (
                    "form",
                    {
                        **RUN_FORM,
                        "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "PASS"}],
                        "bugs": [],
                    },
                )
            ],
        )
        text = report.render_report(d)
        self.assertIn("### 2 a number: PASS\n\n- **TC-2 Reject 131: PASS.**", text)
        self.assertIn("### a list for an id: not covered by a case\n", text)
        self.assertNotIn("2 has no test case", text)

    def test_wrong_typed_run_fields_are_named_not_hidden(self):
        plan = {
            "kind": "ticket",
            "tickets": [{"key": "T-1", "acs": [{"id": "AC-1", "text": "a"}]}],
            "cases": [{"id": "TC-1", "ticket": "T-1", "ac": "AC-1", "title": "x", "route": "/f"}],
        }
        d = self.rundir("2026-11-23-types", plan=plan, runs=[("f", {"cases": "TC-1 PASS", "questions": "why?"})])
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("## Problems reading this folder", r)
        self.assertIn("`cases` should be a list", r)

    def test_one_broken_folder_does_not_break_the_index(self):
        good = self.rundir("2026-11-23-good", plan=PLAN)
        bad = self.rundir("2026-11-22-bad", plan=PLAN)
        real = report.run_summary
        with (
            mock.patch.object(
                report,
                "run_summary",
                lambda p: (_ for _ in ()).throw(TypeError("x")) if p.name == bad.name else real(p),
            ),
            mock.patch.object(report, "render_report_parts", side_effect=TypeError("boom")),
        ):
            self.assertEqual(cli("report", str(good))[0], 0)
        self.assertIn("Could not render this report: TypeError: boom", (good / "REPORT.md").read_text())
        idx = (self.proj / "qa-out/README.md").read_text()
        self.assertIn(
            "| 2026-11-22 | [2026-11-22-bad](2026-11-22-bad/REPORT.md) | ? | unreadable (TypeError) | ? |", idx
        )
        self.assertIn("2026-11-23-good", idx)

    def test_index_newest_first(self):
        a = self.rundir("2026-11-20-PROJ-1", plan=PLAN, runs=[("form", RUN_FORM)])
        b = self.rundir("2026-11-23-plan-PROJ-9", plan={**PLAN, "kind": "plan"})
        self.rundir("old-layout-slug")  # no REPORT.md, no plan: not listed
        (self.proj / "qa-out/form").mkdir()
        (self.proj / "qa-out/form/run.json").write_text("{}")
        report.write_report(a)
        report.write_report(b)
        idx = (self.proj / "qa-out/README.md").read_text()
        rows = [ln for ln in idx.splitlines() if ln.startswith("| 2026")]
        self.assertEqual(
            rows,
            [
                "| 2026-11-23 | [2026-11-23-plan-PROJ-9](2026-11-23-plan-PROJ-9/REPORT.md) | plan | plan only (4 cases) | 0 |",
                "| 2026-11-20 | [2026-11-20-PROJ-1](2026-11-20-PROJ-1/REPORT.md) | ticket | "
                "PROJ-1 ❌ FAIL, PROJ-2 ⛔ BLOCKED | 1 |",
            ],
        )
        self.assertNotIn("old-layout", idx)

    def test_two_bugs_sharing_a_bug_id_are_both_kept_and_named(self):
        """Two page explorers that both wrote BUG-1: neither bug may vanish from the report."""
        b1 = {
            "route": "/form",
            "title": "Speed accepts 131",
            "draft_path": "bugs/BUG-1.md",
            "severity": "LOW",
            "classification": "SUSPECTED",
        }
        b2 = {
            "route": "/list",
            "title": "List sorts wrong",
            "draft_path": "bugs/BUG-1.md",
            "severity": "LOW",
            "classification": "SUSPECTED",
        }
        d = self.rundir(
            "2026-11-23-PROJ-1",
            plan=PLAN1_ANSWERED,
            runs=[
                ("form", {**RUN1, "bugs": [b1]}),
                ("list", {**RUN1, "target": "/list", "cases": [], "bugs": [b2]}),
                ("form-2", {**RUN1, "cases": [], "bugs": [b1]}),
            ],
        )  # the same bug listed again: once
        r = report.render_report(d)
        listed = r.split("\n## Bugs\n")[1].split("\n## ")[0]
        self.assertEqual((listed.count("Speed accepts 131"), listed.count("List sorts wrong")), (1, 1))
        self.assertEqual(listed.count("### BUG-1 "), 2)
        self.assertIn("BUG-1 is also the id of another bug", r.split("## Problems reading this folder")[1])

    def test_ac_ids_match_whatever_their_case_and_unknown_ones_are_named(self):
        plan = {
            **PLAN1_ANSWERED,
            "cases": [{**PLAN1["cases"][0], "ac": "ac-1"}, {**PLAN1["cases"][1], "ac": " AC-2 , AC-9"}],
        }
        d = self.rundir("2026-11-23-PROJ-1", plan=plan, runs=[("form", RUN1)])
        r = report.render_report(d)
        self.assertIn("\n\n✅ PASS · ", r)
        self.assertNotIn("not covered", r)
        self.assertIn("TC-2 names AC-9, which PROJ-1 doesn't have", r.split("## Problems reading this folder")[1])

    def test_page_column_when_a_ticket_spans_pages(self):
        cases = [
            {"id": "TC-1", "route": "/a", "status": "PASS", "actual": "", "notes": ""},
            {"id": "TC-2", "route": "/b", "status": "PASS", "actual": "", "notes": "", "priority": "P1"},
        ]
        self.assertIn("\n  Page: /a\n", report._case_item(cases[0], "/x", True))  # in the case's fold
        self.assertIn("\n  Page: /b · priority P1\n", report._case_item(cases[1], "/x", False))

    def test_header_browser_comes_from_the_runs(self):
        plan = {"kind": "ticket", "env": {"url": "https://qa", "browser": "chrome"}}
        runs = [("run-form.json", {"env": {"url": "https://qa/form", "browser": "firefox"}})]
        self.assertEqual(report._env_line(plan, runs), "https://qa · browser firefox")
        self.assertEqual(report._env_line(plan, []), "https://qa · browser chrome")
        self.assertEqual(report._env_line({}, []), "")

    def test_header_says_what_the_runs_ran_as(self):  # a tenant or role changes what a page shows
        runs = [
            ("run-a.json", {"env": {"url": "https://qa/a", "browser": "chrome", "context": "site North; role editor"}}),
            ("run-b.json", {"env": {"url": "https://qa/b", "browser": "chrome", "context": "site North; role editor"}}),
            (
                "run-c.json",
                {"env": {"url": "https://qa/c", "browser": "chrome", "context": "site South;\nrole viewer"}},
            ),
        ]
        self.assertEqual(report._ran_as(runs), "as site North; role editor / site South; role viewer")
        self.assertEqual(report._ran_as(runs[:0]), "")
        d = self.rundir("2026-11-23-T", plan=PLAN1, runs=[(n[4:-5], {**RUN1, **r}) for n, r in runs])
        self.assertIn(" · as site North; role editor / site South; role viewer\n", report.render_report(d))

    def test_index_write_waits_for_another_report(self):

        d = self.rundir(runs=[("x", {"target": "/x"})])
        fd = os.open(d.parent, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)  # another report is writing the index
        th = threading.Thread(target=report.write_report, args=(d,))
        th.start()
        th.join(0.5)
        self.assertTrue(th.is_alive())
        self.assertFalse((d.parent / "README.md").exists())
        os.close(fd)
        th.join(5)
        self.assertIn(d.name, (d.parent / "README.md").read_text())


class Layout(Rendering):
    """The report layout: the verdict block, what needs action, folds, questions, not-tested groups, case images."""

    def test_verdict_block_action_and_draft_for_a_pass(self):
        d, r = self.render_run([("form", RUN1)], plan=PLAN1_ANSWERED)
        self.assertIn(
            "\n\n✅ PASS · build 1.42 · 2026-11-23 · 2 of 2 cases passed · no bugs\n\nall 2 cases passed\n\n", r
        )
        self.assertIn("1. Post the Jira comment: [jira-comment.txt](jira-comment.txt), after these checks:", r)
        self.assertNotIn("| Ticket", r)  # one ticket: no table
        self.assertLess(r.index("## What to do"), r.index("## Results"))  # actions come before the detail
        self.assertEqual(
            (d / "jira-comment.txt").read_text(),
            "QA result: PASSED on build 1.42 (2026-11-23)\n"
            "Release: Release-A-3.5.0, Release-B-3.5.0\n"
            "Fixed: Speed no longer accepts 131.\n\n"
            "AC-1 Saves 0-130: passed.\n  Saved\n\n"
            "AC-2 Rejects 131: passed.\n  Error shown\n",
        )

    def test_action_block_lists_failures_and_open_questions(self):
        runs = [
            (
                "form",
                {
                    **RUN1,
                    "cases": [
                        {"id": "TC-1", "status": "PASS"},
                        {"id": "TC-2", "status": "FAIL", "actual": "131 saved"},
                    ],
                },
            )
        ]
        _, r = self.render_run(runs)
        todo = r.split("## What to do\n\n")[1].split("\n## ")[0]
        self.assertIn("1. Answer: Q1 [BLOCKING] Is 0 a valid speed? _(from test plan)_\n", todo)
        self.assertIn("\n\nTC-2 failed (AC-2)\n\n", r)  # the failure: named once under the status line
        self.assertEqual(r.count("131 saved"), 1)  # what it saw: once, under its AC
        self.assertIn("### AC-2 Rejects 131: FAIL\n\n- **TC-2 Reject 131: FAIL.** 131 saved\n", r)

    def test_same_question_on_two_pages_is_one_line_and_resolved_ones_fold(self):
        a = {**RUN1, "target": "/a", "cases": [], "questions": ["Is the limit inclusive?"] + RUN1["questions"]}
        b = {**RUN1, "target": "/b", "cases": [], "questions": ["is the limit  inclusive?"]}
        _, r = self.render_run([("a", a), ("b", b)], plan={**PLAN1, "questions": []})
        self.assertEqual(r.lower().count("is the limit inclusive?"), 1)  # merged, case and spaces ignored
        self.assertIn("_(from /a, /b)_", r)
        self.assertIn(
            "<details><summary>Resolved questions (1)</summary>\n\n- Units? — answer: km/h, as the label says.", r
        )

    def test_compact_table_and_folds_open_for_non_pass(self):
        runs = [
            (
                "form",
                {
                    **RUN1,
                    "cases": [
                        {"id": "TC-1", "status": "PASS", "actual": "Saved"},
                        {"id": "TC-2", "status": "FAIL", "actual": "131 saved"},
                    ],
                },
            )
        ]
        _, r = self.render_run(runs)
        self.assertNotIn("| ID", r)  # no case table: each case once, under its AC
        self.assertIn("- **TC-1 Save 130: PASS.** Saved\n\n  <details><summary>Steps</summary>", r)  # passing: closed
        self.assertIn(
            "- **TC-2 Reject 131: FAIL.** 131 saved\n\n  <details open><summary>Steps</summary>", r
        )  # failing: open
        self.assertIn("  Expected: Error shown\n", r)

    def test_case_image_is_linked_and_embedded_only_when_the_file_exists(self):
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        self.assertIn("- **TC-1 Save 130: PASS.** Saved\n", report.render_report(d))
        (d / "evidence").mkdir(exist_ok=True)
        (d / "evidence" / "TC-1.png").write_bytes(b"png")
        r = report.render_report(d)
        self.assertIn("- **TC-1 Save 130: PASS.** Saved · [image](evidence/TC-1.png)\n", r)
        self.assertEqual(r.count("evidence/TC-1.png"), 1)  # linked once, never embedded
        self.assertNotIn("![", r)
        self.assertIn("   - Add TC-1's image (/form) to the comment in Jira.\n", r)
        # annotated=False on a planned case result must surface as "(not highlighted)"
        d2 = self.rundir(
            "2026-11-23-PROJ-1-nohl",
            plan=PLAN1,
            runs=[
                (
                    "form",
                    {
                        **RUN1,
                        "cases": [
                            {"id": "TC-1", "status": "PASS", "actual": "Saved", "annotated": False},
                            {"id": "TC-2", "status": "PASS"},
                        ],
                    },
                )
            ],
        )
        (d2 / "evidence").mkdir(exist_ok=True)
        (d2 / "evidence" / "TC-1.png").write_bytes(b"png")
        self.assertIn("[image](evidence/TC-1.png) (not highlighted)", report.render_report(d2))

    def test_mistagged_bug_table_matches_verdict_ticket(self):
        runs = [
            (
                "form",
                {
                    **RUN1,
                    "bugs": [
                        {
                            "route": "/form",
                            "title": "Broken",
                            "severity": "HIGH",
                            "classification": "CONFIRMED",
                            "ticket": "none",
                        }
                    ],
                },
            )
        ]
        _, r = self.render_run(runs)
        self.assertIn("\n\n❌ FAIL · build 1.42 · 2026-11-23 · 2 of 2 cases passed · 1 bug\n\nBroken HIGH\n\n", r)
        self.assertIn("### Broken (HIGH, CONFIRMED)\n\n- Page /form\n", r)  # PROJ-1's bug, as the verdict counts it
        self.assertNotIn("none", r.split("## Bugs")[1])

    def test_a_folder_named_like_a_case_image_is_not_one(self):
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        (d / "evidence" / "TC-1.png").mkdir(parents=True)
        r = report.render_report(d)
        self.assertIn("- **TC-1 Save 130: PASS.** Saved\n", r)
        self.assertNotIn("evidence/TC-1.png", r)

    def test_case_fold_opens_when_a_case_did_not_pass(self):  # the outer fold was never checked
        runs = [("form", {**RUN1, "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "FAIL"}]})]
        _, r = self.render_run(runs)
        self.assertIn("- **TC-2 Reject 131: FAIL.**\n\n  <details open><summary>Steps</summary>", r)
        self.assertIn("- **TC-1 Save 130: PASS.**\n\n  <details><summary>Steps</summary>", r)
        _, r = self.render_run([("form", RUN1)])
        self.assertNotIn("<details open>", r)

    def test_open_beats_resolved_and_blocking_comes_first(self):
        a = {
            **RUN1,
            "target": "/a",
            "cases": [],
            "questions": [{"text": "Same?", "status": "resolved", "answer": "x"}, "[HIGH] B?", "C?"],
        }
        b = {**RUN1, "target": "/b", "cases": [], "questions": ["same?", "[BLOCKING] A?"]}
        _, r = self.render_run([("a", a), ("b", b)], plan={**PLAN1, "questions": []})
        self.assertNotIn("Resolved questions", r)  # asked open on /b: still open
        opened = r.split("## What to do", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(opened.count("Answer: "), 4)
        self.assertLess(opened.index("[BLOCKING] A?"), opened.index("[HIGH] B?"))
        self.assertLess(opened.index("[HIGH] B?"), opened.index("C?"))
        self.assertIn("Same?", opened)

    def test_plan_only_lists_a_resolved_question_once(self):  # once bare (looked open), once answered
        plan = {**PLAN1, "kind": "plan", "questions": [{"text": "Units?", "status": "resolved", "answer": "km/h"}]}
        r = report.render_report(self.rundir("2026-11-23-PROJ-1", plan=plan))
        self.assertEqual(r.count("Units?"), 1, r)
        self.assertIn("- Units? — answer: km/h", r)

    def test_bug_evidence_naming_a_folder_has_no_image(self):
        bugs = [
            {"title": t, "severity": "LOW", "classification": "CONFIRMED", "evidence": ev}
            for t, ev in (("b1", ".."), ("b2", "."), ("b3", "/"))
        ]
        r = report.render_report(
            self.rundir(
                "2026-11-24-r", runs=[("f", {"mode": "explore", "target": "/f", "status": "COMPLETED", "bugs": bugs})]
            )
        )
        self.assertIn("### b1 (LOW, CONFIRMED)\n\n- Page /f\n", r)
        self.assertNotIn("[image](evidence/", r)

    def test_a_draft_no_run_file_lists_is_an_earlier_bug_in_the_history(
        self,
    ):  # --resume replaced the run that found it
        found = {
            **RUN1,
            "status": "INCOMPLETE",
            "bugs": [
                {
                    "route": "/form",
                    "title": "Save drops data",
                    "severity": "HIGH",
                    "classification": "CONFIRMED",
                    "draft_path": "bugs/BUG-1.md",
                }
            ],
        }
        d, r = self.render_run([("form", found)], plan=PLAN1_ANSWERED)
        self.assertIn("\n\n❌ FAIL · ", r)
        (d / "bugs/BUG-1.md").write_text("# Save drops data on /form\n\nbody\n")
        (d / "bugs/BUG-3.md").write_text("# Named by a case\n")
        (d / "bugs/notes.md").write_text("# not a draft\n")
        (d / ".data/run-form.json").write_text(
            json.dumps({**found, "cases": [{**RUN1["cases"][0], "bug": "see BUG-3"}, RUN1["cases"][1]]})
        )
        report.write_report(d)
        self.assertNotIn("did not see", (d / "REPORT.md").read_text())  # its bugs or a case name both: not repeated
        (d / "bugs/BUG-3.md").unlink()
        (d / "bugs/BUG-2.md").write_text("# Toast <b>overlaps</b> Save\n")
        (d / ".data/run-form.json").write_text(json.dumps(RUN1))  # the resumed explorer didn't see it again
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("\n\n✅ PASS · ", r)  # drafts alone never change a verdict
        self.assertIn(
            "## History\n\n| Date |",  # this run's line first
            r,
        )
        self.assertIn(
            "\n\nEarlier bugs this run did not see (no verdict counts them; file one if it still applies):\n\n"
            "- [BUG-1](bugs/BUG-1.md) Save drops data on /form\n- [BUG-2](bugs/BUG-2.md) Toast &lt;b>overlaps&lt;/b> Save\n",
            r,
        )
        self.assertNotIn("not a draft", r)
        self.assertNotIn("File the bug", r)  # no verdict counts them: in the history, not in What to do

    def test_test_data_left_in_the_app_is_something_to_act_on(self):  # it was only in the folded exploration notes
        run = {**RUN1, "notes": "Explored the form.\nLeft in the app: qa-sdet-zone-1 (delete refused: budget used up)"}
        _, r = self.render_run([("form", run)], plan=PLAN1_ANSWERED)
        sec = r[r.index("## What to do") : r.index("## Results")]
        self.assertIn(
            "1. Delete the test data left in the app on /form: qa-sdet-zone-1 (delete refused: budget used up)\n", sec
        )

    def test_nothing_left_in_the_app_is_nothing_to_do(self):  # a live run wrote "Left in the app: none."
        for none in ("none", "None.", "nothing", "n/a", "-", "none. Browser closed.", "nothing (no write was sent)"):
            run = {**RUN1, "notes": f"Explored the form.\nLeft in the app: {none}"}
            _, r = self.render_run([("form", run)], plan=PLAN1_ANSWERED)
            self.assertNotIn("Delete the test data", r, none)

    def test_a_known_bug_seen_again_is_not_an_earlier_bug_this_run_did_not_see(self):
        # a full re-run names the earlier bug by its memory hash, with the title the card gave it: its draft stays
        known = {
            "hash": "f1ba5beb8c76",
            "title": "Speed accepts  131",
            "severity": "HIGH",
            "classification": "CONFIRMED",
        }
        run = {**RUN1, "bugs": [known]}
        d, _ = self.render_run([("form", run)], plan=PLAN1_ANSWERED)
        (d / "bugs/BUG-1.md").write_text("# Speed accepts 131\n\nbody\n")
        (d / "bugs/BUG-2.md").write_text("# Another earlier bug\n")
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        tail = r[r.index("did not see") :]
        self.assertNotIn("BUG-1", tail)
        self.assertIn("- [BUG-2](bugs/BUG-2.md) Another earlier bug\n", tail)

    def test_bug_video_is_linked_only_when_the_file_exists(self):
        run = {
            "mode": "explore",
            "target": "/f",
            "status": "COMPLETED",
            "bugs": [
                {"title": t, "severity": "HIGH", "classification": "CONFIRMED", "draft_path": f"/x/bugs/BUG-{n}.md"}
                for n, t in ((1, "image and video"), (2, "video only"), (3, "folder named like a video"))
            ],
        }
        d = self.rundir("2026-11-24-v", runs=[("f", run)])
        (d / "evidence/BUG-1.png").write_bytes(b"png")
        (d / "evidence/BUG-1.webm").write_bytes(b"webm")
        (d / "evidence/BUG-2.webm").write_bytes(b"webm")
        (d / "evidence/BUG-3.webm").mkdir()
        r = report.render_report(d)
        self.assertIn(
            "### BUG-1 image and video (HIGH, CONFIRMED)\n\n- Page /f\n- [image](evidence/BUG-1.png) · [video](evidence/BUG-1.webm) · ⚠",
            r,
        )
        self.assertIn("### BUG-2 video only (HIGH, CONFIRMED)\n\n- Page /f\n- [video](evidence/BUG-2.webm) · ⚠", r)
        self.assertIn(
            "### BUG-3 folder named like a video (HIGH, CONFIRMED)\n\n- Page /f\n- ⚠ no draft", r
        )  # the badge stays

    def test_odd_not_tested_shapes(self):  # a string listed its letters; a list category raised TypeError
        _, r = self.render_run([("form", {**RUN1, "not_tested": "keyboard"})])
        self.assertNotIn("## Not tested", r)  # each case alone: one failure must not hide the other
        self.assertIn("run-form.json: `not_tested` should be a list", r)
        _, r = self.render_run(
            [("b", {**RUN1, "target": "/b", "not_tested": [{"category": ["safety"], "text": "t1"}]})]
        )
        self.assertIn("<details><summary>Other (1)</summary>\n\n- t1", r)

    def test_not_tested_groups_by_category_in_order(self):
        items = [
            {"category": "heuristic", "text": "h1"},
            "plain",
            {"category": "safety", "text": "s1"},
            {"category": "safety", "text": "s2"},
            {"category": "bogus", "text": "b1"},
            {"category": "out_of_scope", "text": "o1"},
        ]
        _, r = self.render_run([("form", {**RUN1, "not_tested": items})])
        sec = r[r.index("## Not tested") :]
        heads = [
            "Safety rules (not done on purpose) (2)",
            "Out of scope for this ticket (1)",
            "Heuristics not run (1)",
            "Other (2)",
        ]
        self.assertEqual(
            [sec.index(f"<summary>{h}</summary>") for h in heads],
            sorted(sec.index(f"<summary>{h}</summary>") for h in heads),
        )
        self.assertIn("<summary>Safety rules (not done on purpose) (2)</summary>\n\n- s1\n- s2", sec)
        self.assertIn("<summary>Other (2)</summary>\n\n- plain\n- b1", sec)  # unknown category: Other
        self.assertNotIn("Not reached", sec)  # empty groups are left out


class Escaping(Rendering):
    """Text from a run or a plan can't break the Markdown or add HTML."""

    def test_text_cannot_break_the_markdown(self):
        plan = {
            "kind": "ticket",
            "title": "T\n## injected",
            "tickets": [{"key": "PROJ-1", "summary": "s\n# H1 | x", "acs": [{"id": "AC-1\n- x", "text": "t"}]}],
            "cases": [{"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1\n- x", "title": "t", "route": "/f"}],
        }
        run = {
            "target": "/f",
            "status": "done\n# X",
            "build": "1\n## b",
            "cases": [{"id": "TC-1", "status": "FAIL", "bug": "BUG-9 | <img src=x>\nnew"}],
            "bugs": [{"hash": "ab|cd\nef", "title": "t", "route": "/f"}],
        }
        d = self.rundir(plan=plan, runs=[("f", run)])
        (d / "bugs/BUG-9.md").write_text("x")
        (d / "evidence/TC-1.png").write_bytes(b"png")  # the case has its image: no evidence gap heading
        r = report.render_report(d)
        headings = [ln for ln in r.splitlines() if ln.startswith("#")]
        self.assertEqual(
            headings,
            [
                "# PROJ-1: s # H1 \\| x",
                "## What to do",
                "## Results",
                "### AC-1 - x t: FAIL",
                "## Bugs",
                "### known bug ab\\|cd ef t (unrated)",
                "## Exploration notes",
            ],
        )
        self.assertIn("\n\n❌ FAIL · build 1 ## b · ", r)  # in a line, not a heading
        self.assertIn(
            "- **TC-1 t: FAIL (BUG-9 \\| &lt;img src=x> new).** [image](evidence/TC-1.png)", r
        )  # not a link: not exactly BUG-n
        self.assertNotIn("](bugs/BUG-9", r)
        self.assertIn("1. Check the bug: known bug ab\\|cd ef t (unrated)\n", r)
        self.assertNotIn("<img", r)
        explore = self.rundir("2026-11-24-x", plan={"kind": "explore", "title": "T\n## injected"}, runs=[("f", run)])
        self.assertTrue(report.render_report(explore).startswith("# QA report: T ## injected\n\nexplore · "))

    def test_hostile_text_stays_text(self):
        """XSS payloads, `<!--`, remote images, a leading ``` and `@@` in run text can't hide or rewire the report."""
        plan = {
            "kind": "ticket",
            "title": "T <!-- x",
            "tickets": [{"key": "PROJ-1", "acs": [{"id": "AC-1", "text": "a"}]}],
            "cases": [
                {"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1", "title": "<script>alert(1)</script>", "route": "/f"}
            ],
        }
        run = {
            "target": "/f",
            "status": "done",
            "notes": "```\nrest",
            "questions": ["q <details>"],
            "cases": [
                {
                    "id": "TC-1",
                    "status": "FAIL",
                    "actual": "saw ![p](https://evil.example/t.png) [here](https://evil.example)",
                }
            ],
            "pages": [
                {"route": "/f", "coverage": [{"target": "x", "heuristic": "<!-- [h](https://e)", "result": "pass"}]}
            ],
            "bugs": [
                {"title": "bad @@ title", "severity": "LOW", "route": "/f", "draft_path": "/x/bugs/BUG-1.md"},
                {"title": "two", "severity": "LOW", "route": "/f", "evidence": "/x/evidence/shot (1).png"},
            ],
        }
        d = self.rundir("2026-11-23-odd name", plan=plan, runs=[("f", run)])
        (d / "bugs/BUG-1.md").write_text("x")
        (d / "evidence/BUG-1.png").write_bytes(b"x")
        (d / "evidence/shot (1).png").write_bytes(b"x")
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertNotIn("<!--", r)
        self.assertNotIn("<script>", r)
        self.assertIn("q &lt;details>", r)  # the run's own text is escaped; our folds are the only real tags
        self.assertIn("&lt;script>alert(1)&lt;/script>", r)
        self.assertIn("saw ![p]\\(https://evil.example/t.png) [here]\\(https://evil.example)", r)  # links stay text
        self.assertIn("— other:", r)  # the coverage line is in the folded page title now
        self.assertIn("\n\\```<br>rest\n", r)
        self.assertIn(
            "### BUG-1 bad @@ title (LOW)\n\n- Page /f\n- [draft](bugs/BUG-1.md) · [image](evidence/BUG-1.png)\n", r
        )
        self.assertIn("### two (LOW)\n\n- Page /f\n- [image](evidence/shot%20%281%29.png)\n", r)
        self.assertIn("--body-file '", r)  # a folder name with a space is quoted inside the command
        self.assertEqual(spacing_problems(r), [])
        idx = (self.proj / "qa-out/README.md").read_text()
        self.assertIn("| [PROJ-1](2026-11-23-odd%20name/REPORT.md) |", idx)  # one ticket: its key; the target quoted

    def test_string_steps_cannot_fence_off_the_report(self):
        plan = {**PLAN1, "cases": [{**PLAN1["cases"][0], "steps": "```js\nalert(1)"}, PLAN1["cases"][1]]}
        _, r = self.render_run([("form", RUN1)], plan=plan)
        self.assertIn("\n  \\```js<br>alert(1)\n", r)  # escaped, inside the case's fold
        self.assertEqual(spacing_problems(r), [])

    def test_summary_lines_are_html_escaped_once(self):  # `\|` showed as typed; a case title was escaped twice
        plan = {**PLAN1, "cases": [{**PLAN1["cases"][0], "title": "a|b <i>x</i> & c\\d"}, PLAN1["cases"][1]]}
        _, r = self.render_run([("form", {**RUN1, "target": "/f|g"})], plan=plan)
        self.assertIn("- **TC-1 a\\|b &lt;i>x&lt;/i> & c\\\\d: PASS.**", r)  # Markdown escapes once, in a list line
        self.assertIn("<summary>/f|g (run-form.json: COMPLETED, build 1.42)</summary>", r)  # HTML escapes in a summary
        self.assertNotIn("<i>", r)

    def test_reason_and_case_lines_escape_case_id_and_ac(self):  # the only place ids reached the report unescaped
        plan = {**PLAN1, "cases": [{**PLAN1["cases"][0], "id": "<img src=x onerror=alert(1)>", "ac": "<b>AC</b>"}]}
        _, r = self.render_run(
            [("form", {**RUN1, "cases": [{"id": "<img src=x onerror=alert(1)>", "status": "FAIL"}]})], plan=plan
        )
        self.assertIn("\n\n&lt;img src=x onerror=alert(1)> failed (&lt;b>AC&lt;/b>)\n\n", r)  # the reason line
        self.assertIn("- **&lt;img src=x onerror=alert(1)> Save 130: FAIL.**", r)
        self.assertNotIn("<img", r)
        self.assertNotIn("<b>", r)


if __name__ == "__main__":
    unittest.main()
