"""REPORT.md and the run index (qa_sdet.report): verdicts, merged cases, layout, escaping and the Jira pass-comment draft."""
import fcntl, json, os, threading, unittest
from unittest import mock

from support import Base, cli, PLAN, RUN_FORM, RUN_LIST
from qa_sdet import core, report

PLAN1 = {"kind": "ticket", "title": "PROJ-1", "created": "2026-11-23T18:00:00Z",
         "env": {"url": "https://qa.example", "browser": "chrome"},
         "tickets": [{"key": "PROJ-1", "summary": "Speed limit form", "fix_version": "3.5",
                      "fixed_note": "Speed no longer accepts 131.",
                      "acs": [{"id": "AC-1", "text": "Saves 0-130"}, {"id": "AC-2", "text": "Rejects 131"}]}],
         "cases": [{"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1", "title": "Save 130", "route": "/form",
                    "priority": "P1", "steps": ["Open form", "Type 130"], "expected": "Saved"},
                   {"id": "TC-2", "ticket": "PROJ-1", "ac": "AC-2", "title": "Reject 131", "route": "/form",
                    "priority": "P1", "steps": ["Type 131"], "expected": "Error shown"}],
         "questions": ["Q1 [BLOCKING] Is 0 a valid speed?"]}
RUN1 = {"mode": "ticket", "target": "/form", "build": "1.42", "status": "COMPLETED", "notes": "Explored.",
        "cases": [{"id": "TC-1", "status": "PASS", "actual": "Saved"}, {"id": "TC-2", "status": "PASS", "actual": "Error shown"}],
        "questions": [{"text": "Units?", "status": "resolved", "answer": "km/h, as the label says."}]}
TEMPLATES = ["Release-A-{version}", "Release-B-{version}"]


class Verdict(unittest.TestCase):
    """report.verdict: PASS, PASS WITH NOTES, FAIL or BLOCKED for one ticket, and which bugs count as its own."""

    def case(self, cid, status, ac="AC-1", route="/form", **kw):
        return {"id": cid, "ticket": "T-1", "ac": ac, "route": route, "status": status, "notes": "", "actual": "", **kw}

    def test_four_outcomes(self):
        v = report.verdict
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS"), self.case("TC-2", "PASS")], []), ("PASS", "all 2 cases passed"))
        self.assertEqual(v({"key": "T-1"}, [self.case("TC-1", "PASS"), self.case("TC-3", "FAIL", "AC-2")],
                           [{"id": "BUG-1", "ticket": "T-1", "severity": "HIGH", "classification": "CONFIRMED"}]),
                         ("FAIL", "TC-3 failed (AC-2); BUG-1 HIGH"))
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS")],
                           [{"id": "BUG-4", "route": "/form", "severity": "CRITICAL", "classification": "CONFIRMED"}])[0],
                         "FAIL")  # untagged bug on one of its routes
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS"), self.case("TC-5", "NOT_RUN", notes="session expired")], []),
                         ("BLOCKED", "TC-5 not run: session expired"))
        self.assertEqual(v("T-1", [self.case("TC-5", "BLOCKED")], [])[0], "BLOCKED")
        same = [self.case(f"TC-{i}", "BLOCKED", notes="browser can't start") for i in range(1, 13)]
        self.assertEqual(v("T-1", same, []), ("BLOCKED", "12 cases (TC-1–TC-12) blocked: browser can't start"))
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS", ticket="T-2")], []), ("BLOCKED", "no test cases"))
        self.assertEqual(v("T-1", [self.case("TC-1", "QUESTION")], []), ("PASS WITH NOTES", "TC-1 open question"))
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS", bug="BUG-2")],
                           [{"id": "BUG-2", "route": "/other", "severity": "HIGH", "classification": "SUSPECTED"}]),
                         ("PASS WITH NOTES", "BUG-2 HIGH suspected"))  # referenced by a case; suspected isn't FAIL
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS")],
                           [{"id": "BUG-9", "route": "/other", "ticket": "T-2", "severity": "HIGH",
                             "classification": "CONFIRMED"}])[0], "PASS")  # another ticket's bug

    def test_known_bug_named_by_hash_belongs_to_that_ticket_only(self):
        cases = [{"id": "TC-1", "ticket": "A-1", "route": "/list", "status": "PASS"},
                 {"id": "TC-2", "ticket": "B-1", "route": "/list", "status": "PASS", "bug": "known 0123456789ab"}]
        for raw in ("0123456789abcdef", "#0123456789ab", "0123456789ab (known)"):
            known = [{"hash": raw, "route": "/list", "severity": "HIGH", "classification": "CONFIRMED", "title": "x"}]
            self.assertEqual(report.verdict("A-1", cases, known)[0], "PASS", raw)
            self.assertEqual(report.verdict("B-1", cases, known)[0], "FAIL", raw)

    def test_multi_ticket_runs(self):
        v = report.verdict
        # shared /form: an untagged bug named by T-2's failing case is T-2's, not T-1's
        cases = [self.case("TC-1", "PASS"), self.case("TC-2", "FAIL", ticket="T-2", bug="BUG-1", actual="131 saved")]
        bug = [{"id": "BUG-1", "route": "/form", "severity": "HIGH", "classification": "CONFIRMED"}]
        self.assertEqual(v("T-1", cases, bug), ("PASS", "all 1 cases passed"))
        self.assertEqual(v("T-2", cases, bug), ("FAIL", "TC-2 failed (AC-1): 131 saved; BUG-1 HIGH"))
        # a case covering two AC covers both; an AC without a case is a note, not a PASS
        t = {"key": "T-1", "acs": [{"id": "AC-1"}, {"id": "AC-2"}, {"id": "AC-3", "text": "x"}]}
        two = [self.case("TC-1", "PASS", ac="AC-1, AC-2")]
        self.assertEqual(v(t, two, []), ("PASS WITH NOTES", "AC-3 has no test case"))
        self.assertEqual(report._ac_lines(t, two, True)[:2], ["- AC-1 — ✅ PASS (TC-1)", "- AC-2 — ✅ PASS (TC-1)"])
        # a route skipped after another explorer was BLOCKED: say which route has no result
        self.assertEqual(v("T-1", [self.case("TC-1", "NOT_RUN", route="/list")], []),
                         ("BLOCKED", "TC-1 not run: no result reported for /list"))
        # a re-sighted known bug (hash, no draft) says so
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS")], [{"hash": "abc123abc123", "route": "/form",
                           "title": "Save 500", "severity": "HIGH", "classification": "CONFIRMED"}]),
                         ("FAIL", "known bug Save 500 HIGH"))

    def test_several_failures_named_not_cut_mid_word(self):  # "TC-3 failed (AC-1): … at 1…; …; +4 more"
        acts = ["Speed 131 shows 'Saved zone \"qa-sdet-zone-c\" at 131 km/h', no error", "55.5 saved", "blank saved as 0",
                "1000 saved"]
        cases = [self.case(f"TC-{i}", "FAIL", ac="AC-1" if i < 4 else "AC-10", actual=a) for i, a in enumerate(acts, 1)]
        bugs = [{"id": f"BUG-{i}", "ticket": "T-1", "severity": "HIGH", "classification": "CONFIRMED"} for i in (1, 2, 3)]
        self.assertEqual(report.verdict("T-1", cases, bugs),
                         ("FAIL", "4 cases failed (TC-1, TC-2, TC-3, TC-4) on AC-1, AC-10; BUG-1 HIGH; BUG-2 HIGH; +1 more"))
        v, why = report.verdict("T-1", cases[:1], [])
        self.assertEqual(why, "TC-1 failed (AC-1): Speed 131 shows 'Saved zone \"qa-sdet-zone-c\" at…")
        stuck = self.case("TC-12", "NOT_RUN", actual="Not testable: faults are off", notes="The 0 seen comes from the 404")
        self.assertEqual(report.verdict("T-1", [stuck], [])[1], "TC-12 not run: Not testable: faults are off")  # why, not a side note
        nine = [self.case(f"TC-{i}", "FAIL") for i in range(1, 10)]
        self.assertEqual(report.verdict("T-1", nine, [])[1], "9 cases failed (TC-1, TC-2, TC-3, TC-4, TC-5, TC-6, +3) on AC-1")

    def test_untagged_or_mistagged_serious_bug_still_fails_the_ticket(self):
        case = {"id": "TC-1", "ticket": "S-1", "route": "/incidents", "status": "PASS", "notes": "", "actual": ""}
        serious = {"severity": "CRITICAL", "classification": "CONFIRMED", "id": "BUG-1"}
        for bug in ({**serious, "route": "/incidents/:id", "target": "/incidents"},  # a record opened from the list page
                    {**serious, "route": "/incidents", "ticket": "none"}, {**serious, "route": "/incidents", "ticket": "s-1 "}):
            self.assertEqual(report.verdict("S-1", [case], [bug])[0], "FAIL", bug)
        other = {**serious, "route": "/elsewhere", "target": "/elsewhere"}
        self.assertEqual(report.verdict("S-1", [case], [other])[0], "PASS")
        self.assertEqual(report.run_bugs([("a", {"target": "/incidents", "bugs": [{"route": "/incidents/4521", "title": "t"}]})])[0]["target"],
                         "/incidents")

    def test_bug_tagged_with_a_plan_ticket_that_has_no_cases_stays_with_it(self):
        case = lambda cid, t: {"id": cid, "ticket": t, "route": "/form", "status": "PASS", "notes": "", "actual": ""}
        cases = [case("TC-1", "S-1"), case("TC-3", "S-2")]
        bug = {"id": "BUG-1", "ticket": "S-3", "route": "/form", "severity": "HIGH", "classification": "CONFIRMED"}
        keys = ["S-1", "S-2", "S-3"]
        self.assertEqual([report.verdict(k, cases, [bug], True, keys)[0] for k in keys], ["PASS", "PASS", "FAIL"])


class MergedCases(Base):
    """Several runs' results for one case become one."""

    def test_most_decisive_result_wins(self):
        plan = {"kind": "ticket", "tickets": [{"key": "PROJ-1"}], "cases": [
            {"id": "TC-1", "ticket": "PROJ-1", "route": "/a"}, {"id": "TC-2", "ticket": "PROJ-1", "route": "/b"},
            {"id": "TC-3", "ticket": "PROJ-1", "route": "/b"}]}
        older = {"target": "/a", "cases": [{"id": "TC-1", "status": "FAIL", "actual": "131 saved"},
                                           {"id": "TC-2", "status": "NOT_RUN"}, {"id": "TC-3", "status": "BLOCKED"}]}
        newer = {"target": "/b", "cases": [{"id": "TC-2", "status": "PASS"}, {"id": "TC-3", "status": "PASS"},
                                           {"id": "TC-1", "status": "NOT_RUN", "notes": "other route"}]}
        d = report.load_run(self.rundir(plan=plan, runs=[("a", older), ("b", newer)]))
        cases = report.merged_cases(d["plan"], d["runs"])
        self.assertEqual([(c["id"], c["status"], c["actual"]) for c in cases],
                         [("TC-1", "FAIL", "131 saved"), ("TC-2", "PASS", ""), ("TC-3", "PASS", "")])
        self.assertEqual(report.verdict("PROJ-1", cases, []), ("FAIL", "TC-1 failed: 131 saved"))
        # same rank: the newest run wins
        tie = report.merged_cases(plan, [("a", {"cases": [{"id": "TC-1", "status": "PASS", "actual": "old"}]}),
                                     ("b", {"cases": [{"id": "TC-1", "status": "pass", "actual": "new"}]})])
        self.assertEqual(tie[0]["actual"], "new")

    def test_a_float_case_id_ingest_accepts_is_reported(self):  # core.txt takes 1.0; the report dropped it silently
        cases = report.merged_cases({}, [("a", {"cases": [{"id": 1.0, "status": "FAIL", "actual": "x"}]})])
        self.assertEqual([(c["id"], c["status"]) for c in cases], [("1.0", "FAIL")])

    def test_case_without_or_with_a_repeated_id_is_named_not_dropped_silently(self):
        plan = {"kind": "ticket", "tickets": [{"key": "PROJ-1", "acs": [{"id": "AC-1"}, {"id": "AC-2"}]}], "cases": [
            {"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1", "title": "Save 130"},
            {"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-2", "title": "Reject 131"},  # added by hand in --review
            {"ticket": "PROJ-1", "ac": "AC-2", "title": "Reject 0"}]}
        d = self.rundir(plan=plan)
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("## Problems reading this folder", r)
        self.assertIn("- plan.json: case 'Reject 131' repeats id TC-1; it is left out of this report: give it an id "
                      "of its own (e.g. TC-99)", r)
        self.assertIn("- plan.json: case 'Reject 0' has no id;", r)

    def test_numeric_id_zero_is_an_id(self):
        warnings = []
        cases = report.merged_cases({"cases": [{"id": 0, "title": "zero"}]}, [("a", {"cases": [{"id": 0, "status": "PASS"}]})],
                                warnings)
        self.assertEqual([(c["id"], c["status"]) for c in cases], [("0", "PASS")])
        self.assertEqual(warnings, [])


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
        lines = r.splitlines()
        self.assertEqual(lines[0], "# QA report: PROJ-1, PROJ-2")
        self.assertEqual(lines[1], "ticket · 2026-11-23 · https://qa.example · build 1.42 · browser chrome")
        self.assertIn("| PROJ-1 — Speed limit form | ❌ FAIL | TC-2 failed (AC-2): 131 saved; BUG-1 HIGH | 1 / 1 / 0 | 1 |", r)
        self.assertIn("| PROJ-2 — Incident list | ⚠️ PASS WITH NOTES | TC-4 open question; BUG-2 LOW | 1 / 0 / 1 | 1 |", r)
        self.assertIn("## PROJ-1 — Speed limit form\n**Verdict: ❌ FAIL** — TC-2 failed (AC-2): 131 saved; BUG-1 HIGH", r)
        self.assertIn("- AC-1 Saves 0-130 — ✅ PASS (TC-1)", r)
        self.assertIn("- AC-2 Rejects 131 — ❌ FAIL (TC-2)", r)
        self.assertIn("- AC-1 Sorted by date — ❓ QUESTION (TC-3, TC-4)", r)
        self.assertIn("- **Scope:** speed field\n- **Risks:**\n  - rounding\n- **Out of scope:**\n  - mobile", r)
        # the plan set a priority (P1 first): a Pri column; one start page per ticket: no Page column
        self.assertIn("| ID | Pri | AC | Case | Status | Actual | Evidence |", r)
        self.assertIn("| TC-1 | P1 | AC-1 | Save 130 | ✅ PASS | Saved | — |", r)
        self.assertIn("| TC-2 |  | AC-2 | Reject 131 | ❌ FAIL | 131 saved [BUG-1](bugs/BUG-1.md) | — |", r)
        self.assertIn("| [BUG-1](bugs/BUG-1.md) | HIGH | CONFIRMED | Speed accepts 131 | /form | PROJ-1 | "
                      "[image](evidence/BUG-1.png) |", r)
        self.assertIn("| [BUG-2](bugs/BUG-2.md) | LOW | CONFIRMED | Header misaligned | /list | PROJ-2 | — |", r)
        self.assertIn("### Open questions (2)\n- Is 0 a valid speed? _(from /form)_\n"
                      "- TC-4: Order of ties unspecified _(from /list)_", r)
        self.assertIn("<details><summary>Other (1)</summary>\n\n- Keyboard only: out of time", r)
        self.assertIn("<details><summary>/form (run-form.json: COMPLETED, build 1.42) — boundary ❌, happy-path ✅</summary>\n\n"
                      "Explored the speed field.", r)
        self.assertIn("<details><summary>/list (run-list.json: COMPLETED, build 1.42)</summary>", r)
        order = [r.index(h) for h in ("## Summary", "## Needs your action", "## PROJ-1", "## PROJ-2",
                                      "## Not tested", "## Exploration notes")]
        self.assertEqual(order, sorted(order))

    def test_nothing_run_yet(self):
        d = self.rundir(plan=PLAN)
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertEqual(r.count("⏸️ not run"), 4 + 3)  # every case, and every covered AC
        self.assertIn("| PROJ-1 — Speed limit form | ⏳ NOT STARTED | testing hasn't started yet | 0 / 0 / 2 | 0 |", r)
        self.assertIn("build not run yet", r)
        for absent in ("## Bugs", "## Exploration notes", "## Questions"):
            self.assertNotIn(absent, r)

    def test_empty_folder_and_broken_files(self):
        d = self.rundir("2026-11-20-x")
        self.assertIn("Nothing recorded yet", report.render_report(d))
        (d / ".data/plan.json").write_text("{nope")
        (d / ".data/run-a.json").write_text("[1]")
        r = report.render_report(d)
        self.assertIn("## Problems reading this folder\n- plan.json unreadable (JSONDecodeError)\n- run-a.json is not an object", r)
        self.assertEqual(cli("report", str(self.proj))[0], 2)  # not a qa-out/<run> folder

    def test_explore_kind(self):
        run = {**RUN_FORM, "mode": "explore", "cases": [], "bugs": [{**RUN_FORM["bugs"][0], "ticket": None}]}
        d = self.rundir("2026-11-23-form", plan={"kind": "explore", "title": "/form", "env": {"url": "http://localhost:8765"}},
                        runs=[("form", run)])
        r = report.render_report(d)
        self.assertIn("## Summary\n**Status:** /form COMPLETED · 1 bug(s)", r)
        self.assertNotIn("Verdict", r)
        self.assertNotIn("| Ticket | Verdict", r)
        self.assertIn("| BUG-1 | HIGH | CONFIRMED | Speed accepts 131 | /form |  | — |", r)  # no draft file: no link
        self.assertIn("## Exploration notes", r)

    def test_plan_kind(self):
        d = self.rundir("2026-11-23-plan-PROJ-1", plan={**PLAN, "kind": "plan"})
        r = report.render_report(d)
        self.assertIn("| PROJ-1 — Speed limit form | 2 | 2 |", r)
        self.assertIn("| ID | AC | Case |\n|---|---|---|", r)
        self.assertNotIn("Status", r)
        self.assertNotIn("Verdict", r)
        self.assertIn("- AC-1 Saves 0-130 (TC-1)", r)

    def test_plan_questions_render(self):
        q = {**PLAN, "kind": "plan", "questions": ["Q1 [BLOCKING] Is 0 a valid speed?", "Q2 [LOW] Units?"]}
        r = report.render_report(self.rundir("2026-11-23-plan-PROJ-1", plan=q))
        self.assertIn("## Questions\n- Q1 [BLOCKING] Is 0 a valid speed?\n- Q2 [LOW] Units?", r)
        d = self.rundir(plan={**q, "kind": "ticket"}, runs=[("form", RUN_FORM)])
        # a run's follow-up on plan Q1 read as a second, unnumbered Q1 in one flat list: two labelled lists
        r = report.render_report(d)
        self.assertIn("- Q1 [BLOCKING] Is 0 a valid speed? _(from test plan)_", r)  # a run's follow-up is one list now
        self.assertIn("- Q2 [LOW] Units? _(from test plan)_", r)
        self.assertIn("## Exploration notes\nHeuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked", r)

    def test_odd_field_types_never_crash(self):
        t = [{"key": "PROJ-1", "summary": "A", "acs": [{"id": "AC-1", "text": "x"}]}]
        c = [{"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1", "title": "t", "route": "/f", "steps": ["a"]}]
        plans = [{"kind": "ticket", "tickets": t, "cases": c, "env": {"url": 5}},
                 {"kind": "ticket", "tickets": t, "cases": c, "env": {"url": ["x"], "browser": {"a": 1}}},
                 {"kind": "ticket", "tickets": [{"key": ["a"]}, {"key": 12}], "cases": [{"id": ["x"], "ticket": 12}]},
                 {"kind": "plan", "created": 2026, "title": ["x"], "tickets": t, "cases": {"a": 1}, "questions": "q"},
                 {"kind": ["x"], "tickets": "x", "cases": [{**c[0], "steps": "do | it\nnow", "ac": ["x"]}]}]
        runs = [{"env": {"url": 7, "browser": 3}, "target": 5, "status": ["x"], "build": {"b": 1}},
                {"bugs": [{"ticket": ["x"], "title": {"a": 1}, "route": "/f", "hash": 5}]},
                {"cases": [{"id": ["x"], "status": "PASS"}, {"id": {"a": 1}, "status": 3}, {"id": "TC-1", "bug": 9}]},
                {"pages": [{"route": "/f", "coverage": [{"heuristic": 5, "result": ["x"]}]}], "notes": ["n"]}]
        for i, plan in enumerate(plans):
            d = self.rundir(f"2026-11-23-fuzz{i}", plan=plan, runs=[(f"r{k}", r) for k, r in enumerate(runs)])
            code, _, err = cli("report", str(d))
            self.assertEqual((code, err), (0, ""), plan)
            self.assertTrue((d / "REPORT.md").read_text().startswith("# QA report: "), plan)
            self.assertNotIn("Could not render", (d / "REPORT.md").read_text(), plan)  # rendered, not the fallback
        self.assertEqual((self.proj / "qa-out/README.md").read_text().count("| 2026-11-23 |"), len(plans))

    def test_odd_ac_ids_do_not_lose_the_report(self):
        plan = json.loads(json.dumps(PLAN))
        plan["tickets"][0]["acs"] = [{"id": ["AC-1"], "text": "a list for an id"}, {"id": 2, "text": "a number"}]
        plan["cases"][1]["ac"] = 2
        d = self.rundir(plan=plan, runs=[("form", {**RUN_FORM, "cases": [{"id": "TC-1", "status": "PASS"},
                                                                         {"id": "TC-2", "status": "PASS"}], "bugs": []})])
        text = report.render_report(d)
        self.assertIn("- 2 a number — ✅ PASS (TC-2)", text)
        self.assertIn("a list for an id — not covered", text)
        self.assertNotIn("2 has no test case", text)

    def test_wrong_typed_run_fields_are_named_not_hidden(self):
        plan = {"kind": "ticket", "tickets": [{"key": "T-1", "acs": [{"id": "AC-1", "text": "a"}]}],
                "cases": [{"id": "TC-1", "ticket": "T-1", "ac": "AC-1", "title": "x", "route": "/f"}]}
        d = self.rundir("2026-11-23-types", plan=plan, runs=[("f", {"cases": "TC-1 PASS", "questions": "why?"})])
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("## Problems reading this folder", r)
        self.assertIn("`cases` should be a list", r)

    def test_one_broken_folder_does_not_break_the_index(self):
        good = self.rundir("2026-11-23-good", plan=PLAN)
        bad = self.rundir("2026-11-22-bad", plan=PLAN)
        real = report.run_summary
        with mock.patch.object(report, "run_summary", lambda p: (_ for _ in ()).throw(TypeError("x")) if p.name == bad.name
                               else real(p)), mock.patch.object(report, "render_report_parts", side_effect=TypeError("boom")):
            self.assertEqual(cli("report", str(good))[0], 0)
        self.assertIn("Could not render this report: TypeError: boom", (good / "REPORT.md").read_text())
        idx = (self.proj / "qa-out/README.md").read_text()
        self.assertIn("| 2026-11-22 | [2026-11-22-bad](2026-11-22-bad/REPORT.md) | ? | unreadable (TypeError) | ? |", idx)
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
        self.assertEqual(rows, [
            "| 2026-11-23 | [2026-11-23-plan-PROJ-9](2026-11-23-plan-PROJ-9/REPORT.md) | plan | plan only (4 cases) | 0 |",
            "| 2026-11-20 | [2026-11-20-PROJ-1](2026-11-20-PROJ-1/REPORT.md) | ticket | "
            "PROJ-1 ❌ FAIL, PROJ-2 ⛔ BLOCKED | 1 |"])
        self.assertNotIn("old-layout", idx)

    def test_page_column_when_a_ticket_spans_pages(self):
        cases = [{"id": "TC-1", "route": "/a", "status": "PASS", "actual": "", "notes": ""},
                 {"id": "TC-2", "route": "/b", "status": "PASS", "actual": "", "notes": ""}]
        self.assertEqual(report._case_table(cases, "/x", True)[0],
                         "| ID | AC | Page | Case | Status | Actual | Evidence |")
        self.assertEqual(report._case_table(cases[:1], "/x", False)[0], "| ID | AC | Case |")

    def test_header_browser_comes_from_the_runs(self):
        plan = {"kind": "ticket", "env": {"url": "https://qa", "browser": "chrome"}}
        runs = [("run-form.json", {"env": {"url": "https://qa/form", "browser": "firefox"}})]
        self.assertEqual(report._header("/x/qa-out/2026-11-23-T", "ticket", plan, runs)[1],
                         "ticket · 2026-11-23 · https://qa · build not run yet · browser firefox")
        self.assertTrue(report._header("/x/qa-out/2026-11-23-T", "ticket", plan, [])[1].endswith("browser chrome"))

    def test_index_write_waits_for_another_report(self):
        import threading
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


class Rendering(Base):
    """A plan and its runs written to a run folder and rendered."""

    def render_run(self, runs, plan=PLAN1, templates=TEMPLATES):
        if templates is not None:
            self.config(releaseTemplates=templates)
        d = self.rundir("2026-11-23-PROJ-1", plan=plan, runs=runs)
        report.write_report(d)
        return d, (d / "REPORT.md").read_text()


class Layout(Rendering):
    """The report layout: the verdict block, what needs action, folds, questions, not-tested groups, case images."""

    def test_verdict_block_action_and_draft_for_a_pass(self):
        d, r = self.render_run([("form", RUN1)])
        self.assertIn("**Verdict: ✅ PASS** — all 2 cases passed\n", r)
        self.assertIn("Jira comment draft: [jira-comment-PROJ-1.md](jira-comment-PROJ-1.md)", r)
        self.assertNotIn("## Summary", r)  # one ticket: no table
        self.assertLess(r.index("## Needs your action"), r.index("## PROJ-1"))  # actions come before the detail
        draft = (d / "jira-comment-PROJ-1.md").read_text()
        self.assertIn("Passed on Release-A-3.5.0 and Release-B-3.5.0\n", draft)
        self.assertIn("Fixed: Speed no longer accepts 131.", draft)
        self.assertIn("TC-1 - PASS: Save 130\nTC-2 - PASS: Reject 131", draft)

    def test_action_block_lists_failures_and_open_questions(self):
        runs = [("form", {**RUN1, "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "FAIL",
                                                                                  "actual": "131 saved"}]})]
        _, r = self.render_run(runs)
        self.assertIn("### Open questions (1)\n- Q1 [BLOCKING] Is 0 a valid speed? _(from test plan)_", r)
        self.assertIn("### Failed, blocked or not-run cases (1)\n- TC-2 ❌ FAIL (AC-2) — 131 saved · PROJ-1", r)
        self.assertNotIn("Nothing needs your action", r)

    def test_same_question_on_two_pages_is_one_line_and_resolved_ones_fold(self):
        a = {**RUN1, "target": "/a", "cases": [], "questions": ["Is the limit inclusive?"] + RUN1["questions"]}
        b = {**RUN1, "target": "/b", "cases": [], "questions": ["is the limit  inclusive?"]}
        _, r = self.render_run([("a", a), ("b", b)], plan={**PLAN1, "questions": []})
        self.assertEqual(r.lower().count("is the limit inclusive?"), 1)  # merged, case and spaces ignored
        self.assertIn("_(from /a, /b)_", r)
        self.assertIn("<details><summary>Resolved questions (1)</summary>\n\n- Units? — answer: km/h, as the label says.", r)

    def test_compact_table_and_folds_open_for_non_pass(self):
        runs = [("form", {**RUN1, "cases": [{"id": "TC-1", "status": "PASS", "actual": "Saved"},
                                            {"id": "TC-2", "status": "FAIL", "actual": "131 saved"}]})]
        _, r = self.render_run(runs)
        self.assertIn("| ID | Pri | AC | Case | Status | Actual | Evidence |", r)  # no Steps or Expected columns
        self.assertNotIn("| Steps |", r)
        self.assertIn("<details><summary>TC-1 — Save 130</summary>", r)  # passing: closed
        self.assertIn("<details open><summary>TC-2 — Reject 131</summary>", r)  # failing: open
        self.assertIn("**Expected:** Error shown", r)

    def test_case_image_is_linked_and_embedded_only_when_the_file_exists(self):
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        self.assertIn("| ✅ PASS | Saved | — |", report.render_report(d))
        (d / "evidence").mkdir(exist_ok=True)
        (d / "evidence" / "TC-1.png").write_bytes(b"png")
        r = report.render_report(d)
        self.assertIn("| ✅ PASS | Saved | [image](evidence/TC-1.png) |", r)
        self.assertIn("![TC-1](evidence/TC-1.png)", r)
        self.assertIn("1 evidence image(s)", r)
        # annotated=False on a planned case result must surface as "(not highlighted)"
        d2 = self.rundir("2026-11-23-PROJ-1-nohl", plan=PLAN1, runs=[("form", {**RUN1, "cases": [
            {"id": "TC-1", "status": "PASS", "actual": "Saved", "annotated": False}, {"id": "TC-2", "status": "PASS"}]})])
        (d2 / "evidence").mkdir(exist_ok=True)
        (d2 / "evidence" / "TC-1.png").write_bytes(b"png")
        self.assertIn("[image](evidence/TC-1.png) (not highlighted)", report.render_report(d2))

    def test_mistagged_bug_table_matches_verdict_ticket(self):
        runs = [("form", {**RUN1, "bugs": [{"route": "/form", "title": "Broken", "severity": "HIGH",
                                            "classification": "CONFIRMED", "ticket": "none"}]})]
        _, r = self.render_run(runs)
        self.assertIn("**Verdict: ❌ FAIL**", r)
        self.assertIn("| Broken | /form | PROJ-1 |", r)
        self.assertNotIn("| Broken | /form | none |", r)

    def test_a_folder_named_like_a_case_image_is_not_one(self):
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        (d / "evidence" / "TC-1.png").mkdir(parents=True)
        r = report.render_report(d)
        self.assertIn("0 evidence image(s)", r)
        self.assertNotIn("![TC-1]", r)

    def test_case_fold_opens_when_a_case_did_not_pass(self):  # the outer fold was never checked
        runs = [("form", {**RUN1, "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "FAIL"}]})]
        _, r = self.render_run(runs)
        self.assertIn("<details open><summary>Steps, expected results and evidence (2 cases)</summary>", r)
        _, r = self.render_run([("form", RUN1)])
        self.assertIn("<details><summary>Steps, expected results and evidence (2 cases)</summary>", r)

    def test_open_beats_resolved_and_blocking_comes_first(self):
        a = {**RUN1, "target": "/a", "cases": [], "questions": [{"text": "Same?", "status": "resolved", "answer": "x"},
                                                                "[HIGH] B?", "C?"]}
        b = {**RUN1, "target": "/b", "cases": [], "questions": ["same?", "[BLOCKING] A?"]}
        _, r = self.render_run([("a", a), ("b", b)], plan={**PLAN1, "questions": []})
        self.assertNotIn("Resolved questions", r)  # asked open on /b: still open
        opened = r.split("### Open questions (4)", 1)[1].split("###", 1)[0]
        self.assertLess(opened.index("[BLOCKING] A?"), opened.index("[HIGH] B?"))
        self.assertLess(opened.index("[HIGH] B?"), opened.index("C?"))
        self.assertIn("Same?", opened)

    def test_plan_only_lists_a_resolved_question_once(self):  # once bare (looked open), once answered
        plan = {**PLAN1, "kind": "plan", "questions": [{"text": "Units?", "status": "resolved", "answer": "km/h"}]}
        r = report.render_report(self.rundir("2026-11-23-PROJ-1", plan=plan))
        self.assertEqual(r.count("Units?"), 1, r)
        self.assertIn("- Units? — answer: km/h", r)

    def test_bug_evidence_naming_a_folder_has_no_image(self):
        bugs = [{"title": t, "severity": "LOW", "classification": "CONFIRMED", "evidence": ev}
                for t, ev in (("b1", ".."), ("b2", "."), ("b3", "/"))]
        r = report.render_report(self.rundir("2026-11-24-r", runs=[("f", {"mode": "explore", "target": "/f",
                                                                      "status": "COMPLETED", "bugs": bugs})]))
        self.assertIn("| b1 |", r)
        self.assertNotIn("[image](evidence/", r)

    def test_odd_not_tested_shapes(self):  # a string listed its letters; a list category raised TypeError
        _, r = self.render_run([("form", {**RUN1, "not_tested": "keyboard"})])
        self.assertNotIn("## Not tested", r)  # each case alone: one failure must not hide the other
        self.assertIn("run-form.json: `not_tested` should be a list", r)
        _, r = self.render_run([("b", {**RUN1, "target": "/b", "not_tested": [{"category": ["safety"], "text": "t1"}]})])
        self.assertIn("<details><summary>Other (1)</summary>\n\n- t1", r)

    def test_not_tested_groups_by_category_in_order(self):
        items = [{"category": "heuristic", "text": "h1"}, "plain", {"category": "safety", "text": "s1"},
                 {"category": "safety", "text": "s2"}, {"category": "bogus", "text": "b1"},
                 {"category": "out_of_scope", "text": "o1"}]
        _, r = self.render_run([("form", {**RUN1, "not_tested": items})])
        sec = r[r.index("## Not tested"):]
        heads = ["Safety rules (not done on purpose) (2)", "Out of scope for this ticket (1)", "Heuristics not run (1)",
                 "Other (2)"]
        self.assertEqual([sec.index(f"<summary>{h}</summary>") for h in heads],
                         sorted(sec.index(f"<summary>{h}</summary>") for h in heads))
        self.assertIn("<summary>Safety rules (not done on purpose) (2)</summary>\n\n- s1\n- s2", sec)
        self.assertIn("<summary>Other (2)</summary>\n\n- plain\n- b1", sec)  # unknown category: Other
        self.assertNotIn("Not reached", sec)  # empty groups are left out


class Escaping(Rendering):
    """Text from a run or a plan can't break the Markdown or add HTML."""

    def test_text_cannot_break_the_markdown(self):
        plan = {"kind": "ticket", "title": "T\n## injected",
                "tickets": [{"key": "PROJ-1", "summary": "s\n# H1 | x", "acs": [{"id": "AC-1\n- x", "text": "t"}]}],
                "cases": [{"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1\n- x", "title": "t", "route": "/f"}]}
        run = {"target": "/f", "status": "done\n# X", "build": "1\n## b",
               "cases": [{"id": "TC-1", "status": "FAIL", "bug": "BUG-9 | <img src=x>\nnew"}],
               "bugs": [{"hash": "ab|cd\nef", "title": "t", "route": "/f"}]}
        d = self.rundir(plan=plan, runs=[("f", run)])
        (d / "bugs/BUG-9.md").write_text("x")
        r = report.render_report(d)
        headings = [ln for ln in r.splitlines() if ln.startswith("#")]
        self.assertEqual(headings, ["# QA report: T ## injected", "## Needs your action", "### Bugs to file (1)",
                                    "### Failed, blocked or not-run cases (1)", "## PROJ-1 — s # H1 \\| x",
                                    "### Acceptance criteria", "### Test cases", "## Exploration notes"])
        self.assertIn("| ❌ FAIL | BUG-9 \\| &lt;img src=x> new |", r)  # not a link: not exactly BUG-n
        self.assertNotIn("](bugs/BUG-9", r)
        self.assertIn("| known ab\\|cd ef |", r)
        self.assertIn("- AC-1 - x t — ❌ FAIL (TC-1)", r)
        self.assertIn("## PROJ-1 — s # H1 \\| x", r)

    def test_hostile_text_stays_text(self):
        """XSS payloads, `<!--`, remote images, a leading ``` and `@@` in run text can't hide or rewire the report."""
        plan = {"kind": "ticket", "title": "T <!-- x", "tickets": [{"key": "PROJ-1", "acs": [{"id": "AC-1", "text": "a"}]}],
                "cases": [{"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1", "title": "<script>alert(1)</script>",
                           "route": "/f"}]}
        run = {"target": "/f", "status": "done", "notes": "```\nrest", "questions": ["q <details>"],
               "cases": [{"id": "TC-1", "status": "FAIL", "actual": "saw ![p](https://evil.example/t.png) [here](https://evil.example)"}],
               "pages": [{"route": "/f", "coverage": [{"target": "x", "heuristic": "<!-- [h](https://e)", "result": "pass"}]}],
               "bugs": [{"title": "bad @@ title", "severity": "LOW", "route": "/f", "draft_path": "/x/bugs/BUG-1.md"},
                        {"title": "two", "severity": "LOW", "route": "/f", "evidence": "/x/evidence/shot (1).png"}]}
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
        self.assertIn("| [BUG-1](bugs/BUG-1.md) | LOW |  | bad @@ title | /f | PROJ-1 | [image](evidence/BUG-1.png) |", r)
        self.assertIn("| [image](evidence/shot%20%281%29.png) |", r)
        idx = (self.proj / "qa-out/README.md").read_text()
        self.assertIn("| [2026-11-23-odd name](2026-11-23-odd%20name/REPORT.md) |", idx)

    def test_string_steps_cannot_fence_off_the_report(self):
        plan = {**PLAN1, "cases": [{**PLAN1["cases"][0], "steps": "```js\nalert(1)"}, PLAN1["cases"][1]]}
        _, r = self.render_run([("form", RUN1)], plan=plan)
        self.assertIn("\n\\```js<br>alert(1)\n", r)

    def test_summary_lines_are_html_escaped_once(self):  # `\|` showed as typed; a case title was escaped twice
        plan = {**PLAN1, "cases": [{**PLAN1["cases"][0], "title": "a|b <i>x</i> & c\\d"}, PLAN1["cases"][1]]}
        _, r = self.render_run([("form", {**RUN1, "target": "/f|g"})], plan=plan)
        self.assertIn("<summary>TC-1 — a|b &lt;i&gt;x&lt;/i&gt; &amp; c\\d</summary>", r)
        self.assertIn("<summary>/f|g (run-form.json: COMPLETED, build 1.42)</summary>", r)
        self.assertNotIn("<i>", r)

    def test_action_list_escapes_case_id_and_ac(self):  # the only place ids reached the report unescaped
        plan = {**PLAN1, "cases": [{**PLAN1["cases"][0], "id": "<img src=x onerror=alert(1)>", "ac": "<b>AC</b>"}]}
        _, r = self.render_run([("form", {**RUN1, "cases": [{"id": "<img src=x onerror=alert(1)>", "status": "FAIL"}]})],
                               plan=plan)
        self.assertIn("- &lt;img src=x onerror=alert(1)> ❌ FAIL (&lt;b>AC&lt;/b>)", r)
        self.assertNotIn("<img", r)
        self.assertNotIn("<b>", r)


class JiraDraft(Rendering):
    """jira-comment-<KEY>.md for a ticket that passed."""

    def test_fail_has_no_draft_and_a_stale_draft_is_removed(self):
        runs = [("form", {**RUN1, "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "FAIL",
                                                                                  "actual": "131 saved"}]})]
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=runs)
        (d / "jira-comment-PROJ-1.md").write_text("old draft")
        report.write_report(d)
        self.assertFalse((d / "jira-comment-PROJ-1.md").exists())
        r = (d / "REPORT.md").read_text()
        self.assertIn("**Verdict: ❌ FAIL**", r)
        self.assertNotIn("Jira comment draft", r)

    def test_build_check_uses_the_builds_of_the_tickets_own_runs(self):  # another ticket's build hid it
        plan = {**PLAN1, "tickets": PLAN1["tickets"] + [{"key": "PROJ-2", "summary": "List", "fix_version": "3.4",
                                                          "fixed_note": "The list loads."}],
                "cases": PLAN1["cases"] + [{"id": "TC-3", "ticket": "PROJ-2", "title": "List loads", "route": "/list"}]}
        for unk in ("unknown", "Unknown", "unknown (missing footer)", "N/A", "undefined", ""):
            runs = [("form", {**RUN1, "build": unk}),
                    ("list", {**RUN1, "target": "/list", "build": "3.4.0", "cases": [{"id": "TC-3", "status": "PASS"}]})]
            d, _ = self.render_run(runs, plan=plan)
            self.assertIn("- Build: unknown in this run.", (d / "jira-comment-PROJ-1.md").read_text(), unk)
            self.assertNotIn("Build: unknown", (d / "jira-comment-PROJ-2.md").read_text(), unk)

    def test_draft_takes_one_passing_image_per_route(self):  # one route and two passes proved neither
        plan = {**PLAN1, "cases": PLAN1["cases"] + [{"id": "TC-3", "ticket": "PROJ-1", "ac": "AC-1", "title": "List",
                                                     "route": "/list"}]}
        runs = [("form", {**RUN1, "cases": [{"id": "TC-1", "status": "QUESTION", "notes": "Order?"},
                                            {"id": "TC-2", "status": "PASS"}]}),
                ("list", {**RUN1, "target": "/list", "cases": [{"id": "TC-3", "status": "PASS"}]})]
        d = self.rundir("2026-11-23-PROJ-1", plan=plan, runs=runs)
        for cid in ("TC-1", "TC-2", "TC-3"):
            (d / "evidence" / f"{cid}.png").write_bytes(b"png")
        self.config(releaseTemplates=TEMPLATES)
        report.write_report(d)
        draft = (d / "jira-comment-PROJ-1.md").read_text()
        self.assertIn(f"Paste this image: {d}/evidence/TC-2.png (/form)", draft)  # TC-1 is first, but no pass
        self.assertIn(f"Paste this image: {d}/evidence/TC-3.png (/list)", draft)
        self.assertNotIn("TC-1.png", draft)

    def test_draft_lines_stay_single_and_true_is_no_text(self):
        plan = {**PLAN1, "tickets": [{**PLAN1["tickets"][0], "fix_version": True}],
                "cases": [{**c, "route": "/form\ninjected"} for c in PLAN1["cases"]]}
        d, r = self.render_run([("form", {**RUN1, "not_tested": [True, "Real gap"], "questions": [True]})], plan=plan)
        draft = (d / "jira-comment-PROJ-1.md").read_text()
        self.assertIn("Passed on <release name>", draft)
        self.assertIn("screenshot for /form injected", draft)
        self.assertNotIn("- True", r)
        self.assertIn("Real gap", r)
        self.config(releaseTemplates=["Rel-\n{version}"])
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        report.write_report(d)
        self.assertIn("Passed on Rel- 3.5.0\n", (d / "jira-comment-PROJ-1.md").read_text())

    def test_draft_shows_one_image_per_route_and_no_markup(self):
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        (d / "evidence").mkdir(exist_ok=True)
        (d / "evidence" / "TC-1.png").write_bytes(b"png")
        (d / "evidence" / "TC-2.png").write_bytes(b"png")
        self.config(releaseTemplates=TEMPLATES)
        report.write_report(d)
        draft = (d / "jira-comment-PROJ-1.md").read_text()
        self.assertEqual(draft.count("Paste this image:"), 1)  # both cases are on /form: first passing one only
        self.assertIn(f"Paste this image: {d}/evidence/TC-1.png (/form)", draft)
        body = draft.split("---------- paste below ----------", 1)[1]
        self.assertNotRegex(body, r"[|<>*#]|!\[")  # plain text for Jira

    def test_missing_templates_or_fix_version_leave_placeholders(self):
        d = self.rundir("2026-11-23-PROJ-1", plan={**PLAN1, "tickets": [{**PLAN1["tickets"][0], "fix_version": None,
                                                                            "fixed_note": ""}]}, runs=[("form", RUN1)])
        self.config()  # no releaseTemplates at all
        report.write_report(d)
        draft = (d / "jira-comment-PROJ-1.md").read_text()
        self.assertIn("Passed on <release name>", draft)
        self.assertIn("release names (set releaseTemplates in the qa-sdet config)", draft)
        self.assertIn("Fixed: <write one sentence: what was fixed>", draft)
        self.assertIn("Placeholders to fill:", draft)

    def test_pass_with_notes_draft_has_a_notes_line(self):
        runs = [("form", {**RUN1, "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "QUESTION",
                                                                                  "notes": "Order unclear"}]})]
        d, r = self.render_run(runs)
        self.assertIn("**Verdict: ⚠️ PASS WITH NOTES**", r)
        draft = (d / "jira-comment-PROJ-1.md").read_text()
        self.assertIn("TC-2 - QUESTION: Reject 131", draft)
        self.assertIn("Notes: TC-2 open question", draft)

    def test_release_names_fill_the_version(self):
        self.assertEqual(report._release_names(["A-{version}"], "3.5"), (["A-3.5.0"], ""))
        self.assertEqual(report._release_names(["A-{version}"], "3.5.1"), (["A-3.5.1"], ""))
        self.assertEqual(report._release_names(["A-{version}"], None)[1], "the ticket has no single fix version (plan.json fix_version)")
        self.assertEqual(report._release_names("x", "3.5")[1], "set releaseTemplates in the qa-sdet config")

    def test_round2_report_resilience_types_and_prefixes(self):
        # M2: broken symlink and deeply nested run JSON do not crash report or index
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        (d / ".data/run-broken.json").symlink_to(self.t / "missing-target.json")
        (d / ".data/run-deep.json").write_text("[" * 3000 + "]" * 3000)
        r = report.render_report(d)
        self.assertIn("**Verdict: ✅ PASS**", r)
        self.assertIn("run-broken.json unreadable", r)
        self.assertIn("run-deep.json unreadable", r)
        # M5: bool/list case IDs warned & dropped, numeric 0 actual kept, non-string ticket keys ignored in run_summary
        plan_odd = {**PLAN1, "tickets": [{"key": ["bad"]}, {"key": 12}, *PLAN1["tickets"]],
                    "cases": [{"id": True, "title": "bool id"}, {"id": ["x"], "title": "list id"}, *PLAN1["cases"]]}
        run_zero = {**RUN1, "cases": [{"id": "TC-1", "status": "PASS", "actual": 0}, {"id": "TC-2", "status": "PASS", "actual": "ok"}],
                    "pages": [{"route": "/form", "coverage": [{"heuristic": "   ", "result": "pass"}]}]}
        d2 = self.rundir("2026-11-23-ODD", plan=plan_odd, runs=[("form", run_zero)])
        r2 = report.render_report(d2)
        self.assertIn("| TC-1 |", r2)
        self.assertIn("| ✅ PASS | 0 |", r2)
        self.assertNotIn("| True |", r2)
        self.assertNotIn("| ['x'] |", r2)
        self.assertNotIn("other:", r2)
        summary = report.run_summary(d2)
        self.assertNotIn("['bad']", summary[2])
        # L4: non-ASCII jiraKeyPrefix 'ß' does not strip 'SS-' in inbox
        con = core.connect()
        try:
            out = report.inbox(con, self.proj, [{"key": "SS-12", "fields": {"summary": "x", "status": {"name": "Open"}}}], prefix="ß")
            self.assertIn("Next: /qa-sdet:qa-ticket SS-12", out)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
