"""REPORT.md and the run index (qa_sdet.report): verdicts, merged cases, layout, escaping and the Jira pass-comment draft."""
import fcntl, json, os, re, threading, unittest
from unittest import mock

from support import Base, cli, PLAN, RUN_FORM, RUN_LIST
from qa_sdet import core, memory, report

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
PLAN1_ANSWERED = {**PLAN1, "questions": [{"text": "Q1 [BLOCKING] Is 0 a valid speed?", "status": "resolved", "answer": "Yes"}]}
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

    def test_an_open_blocking_question_keeps_its_ticket_from_a_plain_pass(self):  # all cases passed, the rule undecided
        cases = [self.case("TC-1", "PASS"), self.case("TC-2", "PASS", ticket="T-2", route="/list")]
        v = lambda key, *qs, cs=cases: report.verdict(key, cs, [], known=["T-1", "T-2"],
                                                     questions=report._q_items({"questions": list(qs)}, "test plan"))
        both = "Q1 [BLOCKING] Is 130 itself allowed?"  # untagged (a string can't name a ticket): every ticket's
        self.assertEqual([v(k, both) for k in ("T-1", "T-2")], [("PASS WITH NOTES", "Q1 open blocking question")] * 2)
        mine = {"text": "Q2 [BLOCKING] Which role may edit?", "ticket": " t-2"}
        self.assertEqual((v("T-1", mine)[0], v("T-2", mine)), ("PASS", ("PASS WITH NOTES", "Q2 open blocking question")))
        unknown = {**mine, "ticket": "OTHER-9"}  # a key outside the plan is no tag, as for bugs
        self.assertEqual(v("T-1", unknown)[0], "PASS WITH NOTES")
        for not_blocking in ({"text": both, "status": "Resolved", "answer": "Yes"}, "Q3 [HIGH] Units?", "Q4 [LOW] x"):
            self.assertEqual(v("T-1", not_blocking)[0], "PASS", not_blocking)
        self.assertEqual(v("T-1", "[blocking] no number here"),
                         ("PASS WITH NOTES", "open blocking question: [blocking] no number here"))
        # it is named first, so the pass draft's Notes line never cuts it off
        busy = [self.case(f"TC-{i}", "QUESTION") for i in range(1, 5)]
        self.assertTrue(v("T-1", both, cs=busy)[1].startswith("Q1 open blocking question; TC-1 open question"))
        self.assertEqual(v("T-1", both, cs=[self.case("TC-1", "FAIL")])[0], "FAIL")  # worse verdicts still win
        self.assertEqual(report.verdict("T-1", cases, [], started=False,
                                        questions=report._q_items({"questions": [both]}, ""))[0], "NOT STARTED")

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

    def test_a_run_that_ended_blocked_blocks_its_tickets(self):  # its PASS results can't be trusted
        cases = [self.case("TC-1", "PASS")]
        blocked = {"target": "/form", "status": "Blocked", "summary": "session expired, run /qa-sdet:qa-login",
                   "cases": [{"id": "TC-1", "status": "PASS"}]}
        self.assertEqual(report.verdict("T-1", cases, [], runs=[("form", blocked)]),
                         ("BLOCKED", "the run on /form ended BLOCKED: session expired, run /qa-sdet:qa-login"))
        by_id = {**blocked, "target": "https://qa.example/elsewhere"}  # it reported the ticket's case
        self.assertEqual(report.verdict("T-1", cases, [], runs=[("x", by_id)])[0], "BLOCKED")
        other = {"target": "/list", "status": "BLOCKED", "cases": []}  # another ticket's page
        self.assertEqual(report.verdict("T-1", cases, [], runs=[("list", other)])[0], "PASS")
        incomplete = {**blocked, "status": "INCOMPLETE"}  # budget ran out after the cases: they stand
        self.assertEqual(report.verdict("T-1", cases, [], runs=[("form", incomplete)])[0], "PASS")

    def test_severity_and_classification_read_by_their_first_word(self):  # "Confirmed (2/2)" was no CONFIRMED
        cases = [self.case("TC-1", "PASS")]
        bug = {"id": "BUG-1", "route": "/form", "severity": "High ", "classification": "Confirmed (2/2)"}
        self.assertEqual(report.verdict("T-1", cases, [bug]), ("FAIL", "BUG-1 HIGH"))  # no second class in the reason
        self.assertEqual(report.verdict("T-1", cases, [{**bug, "severity": "critical - data loss"}])[0], "FAIL")
        for weak in ({**bug, "classification": "Not confirmed"}, {**bug, "classification": "SUSPECTED"},
                     {**bug, "severity": "MEDIUM (high impact later)"}):
            self.assertEqual(report.verdict("T-1", cases, [weak])[0], "PASS WITH NOTES", weak)


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
        # a PASS from another run never hides a FAIL or a QUESTION, whichever run is newer
        for worse in ("FAIL", "QUESTION"):
            for first, second in ((worse, "PASS"), ("PASS", worse)):
                two = [("a", {"cases": [{"id": "TC-1", "status": first}]}), ("b", {"cases": [{"id": "TC-1", "status": second}]})]
                self.assertEqual(report.merged_cases(plan, two)[0]["status"], worse, (first, second))
        # same rank: the newest run wins
        tie = report.merged_cases(plan, [("a", {"cases": [{"id": "TC-1", "status": "PASS", "actual": "old"}]}),
                                     ("b", {"cases": [{"id": "TC-1", "status": "pass", "actual": "new"}]})])
        self.assertEqual(tie[0]["actual"], "new")

    def test_a_case_belongs_to_its_ticket_however_the_key_is_written(self):  # a FAIL outside the ticket hid it
        plan = {"kind": "ticket", "tickets": [{"key": "PROJ-1"}], "cases": [
            {"id": "TC-1", "ticket": "PROJ-1", "route": "/form"}, {"id": "TC-2", "route": "/form"},
            {"id": "TC-3", "ticket": " proj-1", "route": "/form"}]}
        runs = [("form", {"target": "/form", "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "FAIL"},
                                                       {"id": "TC-3", "status": "PASS"}]})]
        cases = report.merged_cases(plan, runs)
        self.assertEqual([c["ticket"] for c in cases], ["PROJ-1", "PROJ-1", "PROJ-1"])
        self.assertEqual(report.verdict("PROJ-1", cases, [])[0], "FAIL")
        two = {**plan, "tickets": [{"key": "PROJ-1"}, {"key": "PROJ-2"}]}  # two tickets: no guess for TC-2
        self.assertEqual([c.get("ticket") for c in report.merged_cases(two, runs)], ["PROJ-1", None, "PROJ-1"])
        only_run = report.merged_cases(plan, [("form", {"cases": [{"id": "TC-9", "status": "FAIL"}]})])
        self.assertIsNone(only_run[-1].get("ticket"))  # an explorer's own case is never assigned

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

    def test_a_repeated_case_id_blocks_every_ticket_using_it(self):  # B-1's FAIL showed under A-1 and B-1 got a pass draft
        case = lambda cid, t, route: {"id": cid, "ticket": t, "ac": "AC-1", "title": f"{t} {cid}", "route": route}
        plan = {"kind": "ticket", "title": "A-1, B-1", "tickets": [{"key": "A-1", "acs": [{"id": "AC-1"}]},
                                                                  {"key": "B-1", "acs": [{"id": "AC-1"}]},
                                                                  {"key": "C-1", "acs": [{"id": "AC-1"}]}],
                "cases": [case("TC-1", "A-1", "/a"), case("TC-1", "B-1", "/b"), case("TC-2", "B-1", "/b"),
                          case("TC-3", "C-1", "/c")]}
        ran = lambda target, *cases: {"target": target, "build": "1", "status": "COMPLETED",
                                      "cases": [{"id": c, "status": s, "actual": "x"} for c, s in cases]}
        d = self.rundir("2026-11-23-A-1+B-1", plan=plan, runs=[("a", ran("/a", ("TC-1", "PASS"))),
                                                               ("b", ran("/b", ("TC-1", "FAIL"), ("TC-2", "PASS"))),
                                                               ("c", ran("/c", ("TC-3", "PASS")))])
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        why = "TC-1 repeats an id in plan.json: its result can't be told apart"
        self.assertIn(f"| A-1 | ⛔ BLOCKED | {why} |", r)
        self.assertIn(f"| B-1 | ⛔ BLOCKED | {why} |", r)
        self.assertIn("| C-1 | ✅ PASS |", r)  # a ticket with ids of its own is untouched
        passes = sorted(p.name for p in d.glob("jira-comment-*.md") if "Passed on" in p.read_text())
        self.assertEqual(passes, ["jira-comment-C-1.md"])  # the others get a BLOCKED note, never a pass
        self.assertIn("A-1 ⛔ BLOCKED, B-1 ⛔ BLOCKED, C-1 ✅ PASS", (d.parent / "README.md").read_text())  # index agrees
        # a real FAIL of the ticket still wins
        d2 = self.rundir("2026-11-24-A-1+B-1", plan={**plan, "cases": plan["cases"] + [case("TC-4", "B-1", "/b")]},
                         runs=[("b", ran("/b", ("TC-1", "PASS"), ("TC-2", "PASS"), ("TC-4", "FAIL")))])
        report.write_report(d2)
        self.assertIn("| B-1 | ❌ FAIL | TC-4 failed (AC-1): x |", (d2 / "REPORT.md").read_text())

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
                      "[image](evidence/BUG-1.png) · ⚠", r)
        self.assertIn("| [BUG-2](bugs/BUG-2.md) | LOW | CONFIRMED | Header misaligned | /list | PROJ-2 | ⚠ no repro spec", r)
        self.assertIn("### Open questions (2)\n- Is 0 a valid speed? _(from /form)_\n"
                      "- TC-4: Order of ties unspecified _(from /list)_", r)
        self.assertIn("<details><summary>Other (1)</summary>\n\n- Keyboard only: out of time", r)
        self.assertIn("<details><summary>/form (run-form.json: COMPLETED, build 1.42) — boundary ❌, happy-path ✅</summary>\n\n"
                      "Explored the speed field.", r)
        self.assertIn("<details><summary>/list (run-list.json: COMPLETED, build 1.42)</summary>", r)
        order = [r.index(h) for h in ("## Summary", "## Needs your action", "## PROJ-1", "## PROJ-2",
                                      "## Not tested", "## Exploration notes")]
        self.assertEqual(order, sorted(order))

    def test_a_known_bug_resighted_as_run_json_says_counts_like_a_new_one(self):
        """run-json.md: a re-sighting carries the hash and severity its card line shows, classification CONFIRMED. Rated
        HIGH, it fails the ticket (README FAIL rule), as a new HIGH bug would."""
        self.rundir(name="2026-11-22-PROJ-0", runs=[("form", {**RUN1, "run_id": "r-0", "cases": [], "bugs": [
            {"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved", "severity": "HIGH", "classification": "CONFIRMED"}]})])
        memory.ingest_pending(self.proj / "qa-out")
        con = core.connect()
        try:
            line = next(x for x in memory.card(con, "/form").splitlines() if "Speed accepts 131" in x)
        finally:
            con.close()
        h, sev = re.match(r"- ([0-9a-f]{12}) \w+ \[([^\]]+)\]", line).groups()
        resight = {"route": "/form", "title": "Speed accepts 131", "hash": h, "severity": sev, "classification": "CONFIRMED"}
        d = self.rundir(name="2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", {**RUN1, "run_id": "r-1", "bugs": [resight]})])
        self.assertIn("**Verdict: ❌ FAIL** — known bug Speed accepts 131 HIGH", report.render_report(d))

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
        self.assertIn("| BUG-1 | HIGH | CONFIRMED | Speed accepts 131 | /form |  | ⚠ no draft bugs/BUG-1.md", r)  # no link
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
                 {"kind": ["x"], "tickets": "x", "cases": [{**c[0], "steps": "do | it\nnow", "ac": ["x"]}]},
                 {"kind": "ticket", "tickets": t, "cases": [{**c[0], "ticket": ["PROJ-1"]}, {**c[0], "ticket": {"k": 1}},
                                                            {**c[0], "id": "TC-2", "ticket": ["x"]}]}]  # TC-1 twice
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
            self.assertEqual(cli("plan-find", str(self.proj), "--run", str(d))[0], 0, plan)  # --resume/--retest state
        self.assertEqual((self.proj / "qa-out/README.md").read_text().count("| 2026-11-23 |"), len(plans))
        self.assertEqual(cli("plan-find", str(self.proj), "--keys", "PROJ-1")[0], 0)  # prep reads every folder like this
        con = core.connect()
        try:
            self.assertIn("| PROJ-1 |", report.inbox(con, self.proj, [{"key": "PROJ-1", "fields": {"summary": "s"}}]))
        finally:
            con.close()

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

    def test_two_bugs_sharing_a_bug_id_are_both_kept_and_named(self):
        """Two page explorers that both wrote BUG-1: neither bug may vanish from the report."""
        b1 = {"route": "/form", "title": "Speed accepts 131", "draft_path": "bugs/BUG-1.md", "severity": "LOW",
              "classification": "SUSPECTED"}
        b2 = {"route": "/list", "title": "List sorts wrong", "draft_path": "bugs/BUG-1.md", "severity": "LOW",
              "classification": "SUSPECTED"}
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1_ANSWERED,
                        runs=[("form", {**RUN1, "bugs": [b1]}), ("list", {**RUN1, "target": "/list", "cases": [], "bugs": [b2]}),
                              ("form-2", {**RUN1, "cases": [], "bugs": [b1]})])  # the same bug listed again: once
        r = report.render_report(d)
        table = r.split("### Bugs to file")[1].split("\n\n")[0]
        self.assertEqual((table.count("Speed accepts 131"), table.count("List sorts wrong")), (1, 1))
        self.assertIn("BUG-1 is also the id of another bug", r.split("## Problems reading this folder")[1])

    def test_ac_ids_match_whatever_their_case_and_unknown_ones_are_named(self):
        plan = {**PLAN1_ANSWERED, "cases": [{**PLAN1["cases"][0], "ac": "ac-1"}, {**PLAN1["cases"][1], "ac": " AC-2 , AC-9"}]}
        d = self.rundir("2026-11-23-PROJ-1", plan=plan, runs=[("form", RUN1)])
        r = report.render_report(d)
        self.assertIn("**Verdict: ✅ PASS**", r)
        self.assertNotIn("not covered", r)
        self.assertIn("TC-2 names AC-9, which PROJ-1 doesn't have", r.split("## Problems reading this folder")[1])

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
        d, r = self.render_run([("form", RUN1)], plan=PLAN1_ANSWERED)
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

    def test_a_draft_no_run_file_lists_stays_in_the_action_block(self):  # --resume replaced the run file that found it
        found = {**RUN1, "status": "INCOMPLETE", "bugs": [{"route": "/form", "title": "Save drops data", "severity": "HIGH",
                                                           "classification": "CONFIRMED", "draft_path": "bugs/BUG-1.md"}]}
        d, r = self.render_run([("form", found)], plan=PLAN1_ANSWERED)
        self.assertIn("**Verdict: ❌ FAIL**", r)
        (d / "bugs/BUG-1.md").write_text("# Save drops data on /form\n\nbody\n")
        (d / "bugs/BUG-3.md").write_text("# Named by a case\n")
        (d / "bugs/notes.md").write_text("# not a draft\n")
        (d / ".data/run-form.json").write_text(json.dumps({**found, "cases": [{**RUN1["cases"][0], "bug": "see BUG-3"},
                                                                             RUN1["cases"][1]]}))
        report.write_report(d)
        self.assertNotIn("no run file lists", (d / "REPORT.md").read_text())  # its bugs or a case name both: not repeated
        (d / "bugs/BUG-3.md").unlink()
        (d / "bugs/BUG-2.md").write_text("# Toast <b>overlaps</b> Save\n")
        (d / ".data/run-form.json").write_text(json.dumps(RUN1))  # the resumed explorer didn't see it again
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("**Verdict: ✅ PASS**", r)  # drafts alone never change a verdict
        self.assertIn("### Bug drafts no run file lists (2)\n", r)
        self.assertIn("- [BUG-1](bugs/BUG-1.md) Save drops data on /form\n- [BUG-2](bugs/BUG-2.md) Toast &lt;b>overlaps&lt;/b> Save\n", r)
        self.assertNotIn("not a draft", r)
        self.assertNotIn("Nothing needs your action.", r)

    def test_test_data_left_in_the_app_is_something_to_act_on(self):  # it was only in the folded exploration notes
        run = {**RUN1, "notes": "Explored the form.\nLeft in the app: qa-sdet-zone-1 (delete refused: budget used up)"}
        _, r = self.render_run([("form", run)], plan=PLAN1_ANSWERED)
        sec = r[r.index("## Needs your action"):r.index("## PROJ-1")]
        self.assertIn("### Test data left in the app (1)\n- /form: qa-sdet-zone-1 (delete refused: budget used up)\n", sec)

    def test_bug_video_is_linked_only_when_the_file_exists(self):
        run = {"mode": "explore", "target": "/f", "status": "COMPLETED", "bugs": [
            {"title": t, "severity": "HIGH", "classification": "CONFIRMED", "draft_path": f"/x/bugs/BUG-{n}.md"}
            for n, t in ((1, "image and video"), (2, "video only"), (3, "folder named like a video"))]}
        d = self.rundir("2026-11-24-v", runs=[("f", run)])
        (d / "evidence/BUG-1.png").write_bytes(b"png")
        (d / "evidence/BUG-1.webm").write_bytes(b"webm")
        (d / "evidence/BUG-2.webm").write_bytes(b"webm")
        (d / "evidence/BUG-3.webm").mkdir()
        r = report.render_report(d)
        self.assertIn("| image and video | /f |  | [image](evidence/BUG-1.png) · [video](evidence/BUG-1.webm) · ⚠", r)
        self.assertIn("| video only | /f |  | [video](evidence/BUG-2.webm) · ⚠", r)
        self.assertIn("| folder named like a video | /f |  | ⚠ no draft", r)  # no video link; the repro badge stays

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


# verbatim playwright-cli 0.1.24 output of `requests --all` and `console error --all` (fixture map.html, then form.html)
REQUESTS_LOG = """### Result
2. [GET] http://localhost:8765/api/incidents.json => [200] OK
3. [GET] http://localhost:8765/api/closures.json => [404] File not found
4. [GET] http://localhost:8765/api/incidents.json => [200] OK
5. [GET] http://localhost:8765/api/closures.json => [404] File not found
6. [GET] http://localhost:9/api/x?token=abc123secret => [FAILED] net::ERR_UNSAFE_PORT

Note: 2 static requests not shown, run with --static option to see them.
"""
CONSOLE_LOG = """### Result
Total messages: 7 (Errors: 6, Warnings: 1)
Returning 6 messages for level "error"

[ERROR] Failed to load resource: the server responded with a status of 404 (File not found) @ http://localhost:8765/favicon.ico:1
[ERROR] Failed to load resource: the server responded with a status of 404 (File not found) @ http://localhost:8765/api/closures.json:1
[ERROR] Zone save failed: 500 @ :1
[ERROR] Failed to load resource: net::ERR_UNSAFE_PORT @ http://localhost:9/api/x?token=abc123secret:1
TypeError: rows is undefined
    at eval (eval at <anonymous> (eval at evaluate (:370:30)), <anonymous>:1:35)
"""


class Signals(Rendering):
    """Console errors and failed requests the explorer saved before closing the browser (.data/*-<slug>.log)."""

    def signals_run(self, run=RUN1, requests=REQUESTS_LOG, console=CONSOLE_LOG, drafts=()):
        self.config(releaseTemplates=TEMPLATES)
        d = self.rundir("2026-11-23-PROJ-1", plan={**PLAN1, "questions": []}, runs=[("form", run)])  # nothing else to act on
        for kind, text in (("requests", requests), ("console", console)):
            if text is not None:
                (d / f".data/{kind}-form.log").write_text(text)
        for i, text in enumerate(drafts, 1):
            (d / f"bugs/BUG-{i}.md").write_text(text)
        for c in run.get("cases") or []:  # every checked case has its image: only signals are under test
            (d / f"evidence/{c['id']}.png").write_bytes(b"png")
        report.write_report(d)
        return (d / "REPORT.md").read_text()

    def test_unexplained_signals_are_listed_once_with_counts(self):
        r = self.signals_run()
        self.assertIn("### Browser signals no bug explains (4)\n"
                      "- /form: GET /api/closures.json → 404 (×2)\n"
                      "- /form: GET /api/x → FAILED net::ERR_UNSAFE_PORT\n"
                      "- /form: console: Zone save failed: 500\n"
                      "- /form: console: TypeError: rows is undefined\n", r)
        for gone in ("incidents.json", "Failed to load resource", "favicon", "at eval", "abc123secret"):
            self.assertNotIn(gone, r)  # 2xx, network lines the requests log carries, stack lines, a token
        self.assertIn("**Verdict: ✅ PASS**", r)  # signals never change a verdict
        self.assertNotIn("Nothing needs your action.", r)

    def test_a_signal_the_run_or_a_bug_draft_names_is_explained(self):
        r = self.signals_run(run={**RUN1, "notes": "The toast says zone save failed: 500, as the ticket expects."},
                             drafts=["## Evidence\n- Network: `GET /api/closures.json` → 404"])
        sec = r[r.index("## Needs your action"):r.index("## PROJ-1")]
        self.assertIn("### Browser signals no bug explains (2)\n"
                      "- /form: GET /api/x → FAILED net::ERR_UNSAFE_PORT\n"
                      "- /form: console: TypeError: rows is undefined\n", sec)

    def test_a_path_alone_explains_no_failed_request(self):
        """The run's own target, or a path a passing note mentions, must not hide another call failing on it."""
        requests = ("1. [POST] http://localhost:8765/form => [500] Internal Server Error\n"
                    "2. [DELETE] http://localhost:8765/api/zones => [500] Internal Server Error\n"
                    "3. [PUT] http://localhost:8765/api/zones => [500] Internal Server Error\n"
                    "4. [GET] http://localhost:8765/api/zones/7 => [404] Not Found\n")
        r = self.signals_run(run={**RUN1, "notes": "GET /api/zones returns the list."}, requests=requests, console=None,
                             drafts=["## Evidence\n- Network: PUT http://localhost:8765/api/zones → 500"])
        self.assertIn("- /form: POST /form → 500", r)
        self.assertIn("- /form: DELETE /api/zones → 500", r)
        self.assertIn("- /form: GET /api/zones/7 → 404", r)
        self.assertNotIn("PUT /api/zones", r)

    def test_third_party_failures_fold_into_one_line_and_request_loops_show(self):
        self.config(releaseTemplates=TEMPLATES, allowedHosts=["localhost"])
        d = self.rundir("2026-11-23-PROJ-1", plan={**PLAN1, "questions": []}, runs=[("form", RUN1)])
        loop = "".join(f"{i}. [GET] http://localhost:8765/api/zones/{i} => [200] OK\n" for i in range(4, 10))
        (d / ".data/requests-form.log").write_text("1. [POST] http://localhost:8765/form => [500] Error\n"
                                                   "2. [GET] https://cdn.example.com/lib.js => [404] Not Found\n"
                                                   "3. [GET] https://tiles.example.org/1/2/3.png => [FAILED] net::ERR\n" + loop)
        report.write_report(d)
        sec = (d / "REPORT.md").read_text().split("## Needs your action")[1].split("\n## ")[0]
        self.assertIn("- /form: POST /form → 500", sec)
        self.assertIn("- /form: GET /api/zones/:id ×6 (the same call 6 times: a request loop?)", sec)
        self.assertNotIn("cdn.example.com", sec.split("<details>")[0])
        self.assertIn("<summary>2 third-party signal(s) (hosts outside allowedHosts)</summary>", sec)
        code, out, _ = cli("signals-triage", str(d), "--slug", "form")
        self.assertEqual(code, 0)
        self.assertIn("#1 POST /form → 500", out)  # the index for `response-body 1`
        self.assertIn("2 third-party", out)

    def test_no_logs_or_only_explained_ones_leave_nothing_to_act_on(self):
        for requests, console, drafts in ((None, None, ()), ("3. [GET] http://h/api/closures.json => [404] Not found\n",
                                                              None, ["GET /api/closures.json → 404"])):
            r = self.signals_run(requests=requests, console=console, drafts=drafts)
            self.assertNotIn("Browser signals", r)
            # a draft no run file lists is still something to act on (the signal it explains is not)
            self.assertIn("### Bug drafts no run file lists (1)" if drafts else "Nothing needs your action.", r)
            self.assertNotIn("Problems reading this folder", r)

    def test_page_text_in_a_message_stays_text(self):
        r = self.signals_run(requests=None, console="[ERROR] <img src=x onerror=alert(1)> [go](https://evil.example) | x @ :1\n")
        self.assertIn("- /form: console: &lt;img src=x onerror=alert(1)> [go]\\(https://evil.example) \\| x\n", r)

    def test_a_long_list_is_cut_and_says_where_the_rest_is(self):
        r = self.signals_run(requests=None, console="".join(f"[ERROR] row {i} failed @ :1\n" for i in range(30)))
        self.assertIn("### Browser signals no bug explains (30)", r)
        self.assertIn("- /form: console: row 14 failed\n- +15 more: see `requests-*.log` and `console-*.log` in `.data/`\n", r)
        self.assertNotIn("row 15 failed", r)


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
        (d / "evidence/TC-1.png").write_bytes(b"png")  # the case has its image: no evidence gap heading
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
        (d / "jira-comment-PROJ-9.md").write_text("a ticket no longer in the plan")
        report.write_report(d)
        draft = (d / "jira-comment-PROJ-1.md").read_text()  # a rejection now, never the old or a pass draft
        self.assertIn("QA: FAIL", draft)
        self.assertNotIn("Passed on", draft)
        self.assertFalse((d / "jira-comment-PROJ-9.md").exists())  # stale: removed
        r = (d / "REPORT.md").read_text()
        self.assertIn("**Verdict: ❌ FAIL**", r)

    def test_a_blocked_run_gets_no_pass_draft(self):  # its cases said PASS; the run said BLOCKED
        d, r = self.render_run([("form", {**RUN1, "status": "BLOCKED", "summary": "session expired"})])
        self.assertIn("**Verdict: ⛔ BLOCKED** — the run on /form ended BLOCKED: session expired", r)
        self.assertNotIn("Passed on", (d / "jira-comment-PROJ-1.md").read_text())  # a BLOCKED note, never a pass
        self.assertIn("⛔ BLOCKED", (d.parent / "README.md").read_text())  # the run index agrees

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
        self.assertIn("Notes: Q1 open blocking question; TC-2 open question\n", draft)  # PLAN1's Q1 is still open

    def test_release_names_fill_the_version(self):
        self.assertEqual(report._release_names(["A-{version}"], "3.5"), (["A-3.5.0"], ""))
        self.assertEqual(report._release_names(["A-{version}"], "3.5.1"), (["A-3.5.1"], ""))
        self.assertEqual(report._release_names(["A-{version}"], None)[1], "the ticket has no single fix version (plan.json fix_version)")
        self.assertEqual(report._release_names("x", "3.5")[1], "set releaseTemplates in the qa-sdet config")

    def test_round2_report_resilience_types_and_prefixes(self):
        # M2: broken symlink and deeply nested run JSON do not crash report or index
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1_ANSWERED, runs=[("form", RUN1)])
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


class PlanFindAndRetest(Rendering):
    """`qa-mem plan-find` and honest partial-retest reporting (`retest_of` + `prior_passed`)."""

    def test_reusable_plan_includes_zero_execution_blocked_run(self):
        blocked = {"run_id": "t-1", "mode": "ticket", "target": "/form", "build": "unknown",
                   "status": "BLOCKED", "summary": "session expired, run /qa-sdet:qa-login",
                   "cases": [{"id": "TC-1", "status": "BLOCKED"}, {"id": "TC-2", "status": "NOT_RUN"}]}
        d = self.rundir("2026-11-24-PROJ-1", plan=PLAN1, runs=[("form", blocked)])
        code, out, err = cli("plan-find", str(self.proj), "--keys", "PROJ-1")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(data["reusable_plan"]["run_dir"], str(d.resolve()))
        self.assertEqual(data["latest_run"]["run_dir"], str(d.resolve()))
        self.assertTrue(data["latest_run"]["can_resume"])
        self.assertEqual(data["latest_run"]["incomplete_routes"], ["/form"])

    def test_latest_run_resume_and_retest_breakdown(self):
        d = self.rundir("2026-11-24-PROJ-1+PROJ-2", plan=PLAN, runs=[("form", RUN_FORM)])
        code, out, err = cli("plan-find", str(self.proj), "--keys", "PROJ-1,PROJ-2")
        self.assertEqual((code, err), (0, ""))
        lr = json.loads(out)["latest_run"]
        self.assertEqual(lr["run_dir"], str(d.resolve()))
        self.assertEqual(lr["builds"], ["1.42"])
        self.assertEqual(lr["counts"], {"PASS": 1, "FAIL": 1, "BLOCKED": 0, "NOT_RUN": 2, "QUESTION": 0})
        self.assertEqual(lr["incomplete_routes"], ["/list"])
        self.assertEqual(lr["completed_routes"], ["/form"])
        self.assertEqual([c["id"] for c in lr["retest_cases"]], ["TC-2", "TC-3", "TC-4"])
        self.assertEqual([c["id"] for c in lr["passed_cases"]], ["TC-1"])
        self.assertTrue(lr["can_resume"])
        self.assertTrue(lr["can_retest"])
        # Inspect directly by --run
        code2, out2, _ = cli("plan-find", str(self.proj), "--run", str(d))
        self.assertEqual((code2, json.loads(out2)["latest_run"]["run_dir"]), (0, str(d.resolve())))
        # Bad args
        self.assertEqual(cli("plan-find", str(self.proj))[0], 2)
        self.assertEqual(cli("plan-find", str(self.proj), "--keys", "PROJ-1", "--run", str(d))[0], 2)
        self.assertEqual(cli("plan-find", str(self.proj), "--run", str(self.proj / "qa-out/missing"))[0], 2)

    def test_retest_reruns_passes_the_prior_verdict_did_not_trust(self):
        # a partial retest turned a run that ended BLOCKED, or a route's CONFIRMED HIGH bug, into "passed on prior build"
        case = lambda cid, t, route: {"id": cid, "ticket": t, "ac": "AC-1", "title": cid, "route": route}
        plan = {"kind": "ticket", "tickets": [{"key": "PROJ-1", "acs": [{"id": "AC-1"}]}, {"key": "PROJ-2"}],
                "cases": [case("TC-1", "PROJ-1", "/form"), case("TC-2", "PROJ-1", "/list"), case("TC-3", "PROJ-1", "/other"),
                          case("TC-4", "PROJ-1", "/zones"), case("TC-5", "PROJ-1", "/fail"), case("TC-6", "PROJ-2", "/list")]}
        ran = lambda target, status, *cases, bugs=(): {"target": target, "build": "1.0", "status": status, "bugs": list(bugs),
                                                       "cases": [{"id": c, "status": s} for c, s in cases]}
        bug = lambda sev: {"route": "/x", "title": f"{sev} bug", "severity": sev, "classification": "CONFIRMED"}
        prior = self.rundir("2026-11-20-PROJ-1+PROJ-2", plan=plan, runs=[
            ("form", ran("/form", "BLOCKED", ("TC-1", "PASS"))),  # session expired after TC-1: its PASS can't be trusted
            ("list", ran("/list", "COMPLETED", ("TC-2", "PASS"), ("TC-6", "PASS"),
                         bugs=[{**bug("HIGH"), "route": "/list", "ticket": "PROJ-1"}])),  # PROJ-1's bug, not PROJ-2's
            ("other", ran("/other", "COMPLETED", ("TC-3", "PASS"), bugs=[{**bug("MEDIUM"), "route": "/other"}])),
            ("zones", ran("/zones", "INCOMPLETE", ("TC-4", "PASS"))),  # budget ran out after the cases: they stand
            ("fail", ran("/fail", "COMPLETED", ("TC-5", "FAIL")))])
        report.write_report(prior)
        self.assertIn("**Verdict: ❌ FAIL**", (prior / "REPORT.md").read_text())
        lr = json.loads(cli("plan-find", str(self.proj), "--run", str(prior))[1])["latest_run"]
        self.assertEqual([c["id"] for c in lr["retest_cases"]], ["TC-1", "TC-2", "TC-5"])
        self.assertEqual([c["id"] for c in lr["passed_cases"]], ["TC-3", "TC-4", "TC-6"])
        # the retest the skill builds from it: fixing TC-5 alone is no pass while TC-1 and TC-2 wait for a run
        retest = {**plan, "retest_of": prior.name, "cases": [c for c in plan["cases"] if c["id"] in ("TC-1", "TC-2", "TC-5")],
                  "prior_passed": lr["passed_cases"]}
        d = self.rundir("2026-11-21-PROJ-1+PROJ-2", plan=retest, runs=[("fail", ran("/fail", "COMPLETED", ("TC-5", "PASS")))])
        report.write_report(d)
        self.assertIn("| PROJ-1 | ⛔ BLOCKED |", (d / "REPORT.md").read_text())
        self.assertIn("QA: BLOCKED", (d / "jira-comment-PROJ-1.md").read_text())

    def test_retest_trusts_a_pass_exactly_when_the_verdict_does(self):  # one rule for "a run that ended BLOCKED"
        blocked = lambda **kw: {"status": "BLOCKED", "summary": "session expired", "cases": [], **kw}
        rows = [  # (case route, blocked run): does the run cover the case?
            ("/form", blocked(target="/form")),  # its page
            ("/form", blocked(target="https://qa.example/form?x=1")),  # same page, other spelling
            ("/list", blocked(target="/form", cases=[{"id": "TC-1", "status": "PASS"}])),  # it reported the case
            ("/list", blocked(target="/form")),  # another page: no
            ("http://[bad", blocked()),  # an unparseable route and a run with no target
        ]
        for route, run in rows:
            with self.subTest(route=route, run=run):
                plan = {"kind": "ticket", "tickets": [{"key": "PROJ-1"}],
                        "cases": [{"id": "TC-1", "ticket": "PROJ-1", "title": "t", "route": route}]}
                ok = {"target": route, "status": "COMPLETED", "cases": [{"id": "TC-1", "status": "PASS"}]}
                d = self.rundir("2026-11-20-PROJ-1", plan=plan, runs=[("a", ok), ("b", run)])
                cases = report.merged_cases(plan, report.load_run(d)["runs"])
                v = report.verdict({"key": "PROJ-1"}, cases, [], runs=report.load_run(d)["runs"])[0]
                retested = [c["id"] for c in report._run_state(d)["retest_cases"]]
                self.assertEqual((v, retested), ("BLOCKED", ["TC-1"]) if v == "BLOCKED" else ("PASS", []))

    def test_report_index_and_inbox_give_the_same_verdicts(self):  # one place feeds verdict(), not three
        case = lambda cid, t, route: {"id": cid, "ticket": t, "ac": "AC-1", "title": cid, "route": route}
        plan = {"kind": "ticket", "title": "QA-1, QA-2, QA-3, QA-4", "retest_of": "2026-11-20-x",
                "tickets": [{"key": k, "acs": [{"id": "AC-1"}]} for k in ("QA-1", "QA-2", "QA-3", "QA-4")],
                "cases": [case("TC-1", "QA-1", "/a"), case("TC-2", "QA-2", "/b"), case("TC-4", "QA-4", "/d")],
                "prior_passed": [{**case("TC-3", "QA-3", "/c"), "build": "1.0"}],  # QA-3: only a prior-build pass
                "questions": [{"text": "Q1 [BLOCKING] Which role?", "ticket": "QA-1"}]}  # QA-1: an open blocking question
        ran = lambda t, s, c: {"target": t, "build": "1.1", "status": s, "cases": [{"id": c, "status": "PASS"}]}
        d = self.rundir("2026-11-24-QA-1+QA-2+QA-3+QA-4", plan=plan,  # QA-2: its run ended BLOCKED; QA-4: a plain pass
                        runs=[("a", ran("/a", "COMPLETED", "TC-1")), ("b", ran("/b", "BLOCKED", "TC-2")),
                              ("d", ran("/d", "COMPLETED", "TC-4"))])
        report.write_report(d)
        want = {"QA-1": "⚠️ PASS WITH NOTES", "QA-2": "⛔ BLOCKED", "QA-3": "⚠️ PASS WITH NOTES", "QA-4": "✅ PASS"}
        r = (d / "REPORT.md").read_text()
        self.assertEqual({k: re.search(rf"^\| {k} \| ([^|]+) \|", r, re.M).group(1) for k in want}, want)
        self.assertIn(", ".join(f"{k} {v}" for k, v in want.items()), (d.parent / "README.md").read_text())
        con = core.connect()
        try:
            inbox = report.inbox(con, self.proj, [{"key": k, "fields": {"summary": "s"}} for k in want])
        finally:
            con.close()
        self.assertEqual({k: re.search(rf"\| {k} \|.*\| ([^|]+) \|$", inbox, re.M).group(1) for k in want}, want)

    def test_a_chained_retest_keeps_the_earlier_prior_passes(self):
        """Run 1 -> retest 2 -> retest 3: what retest 2 carried as prior passes must reach retest 3's plan."""
        plan = {**PLAN1, "retest_of": "r1", "cases": [PLAN1["cases"][1]], "prior_passed": [{**PLAN1["cases"][0], "build": "1.42"}]}
        d = self.rundir("2026-11-24-PROJ-1", plan=plan,
                        runs=[("form", {**RUN1, "build": "1.43", "cases": [{"id": "TC-2", "status": "FAIL", "actual": "x"}]})])
        st = report._run_state(d)
        self.assertEqual([(c["id"], c["build"]) for c in st["passed_cases"]], [("TC-1", "1.42")])
        self.assertEqual([c["id"] for c in st["retest_cases"]], ["TC-2"])

    def test_a_ticket_carried_only_by_prior_passes_reads_honestly(self):
        """Multi-ticket partial retest: PROJ-2 was not re-run at all. No 0/0/0, no 'unknown build in this run'."""
        plan = {**PLAN, "retest_of": "2026-11-23-PROJ-1+PROJ-2", "cases": [c for c in PLAN["cases"] if c["ticket"] == "PROJ-1"],
                "questions": [],
                "prior_passed": [{**c, "build": "1.42"} for c in PLAN["cases"] if c["ticket"] == "PROJ-2"]}
        prior = self.rundir("2026-11-23-PROJ-1+PROJ-2")
        pid = plan["prior_passed"][0]["id"]
        (prior / f"evidence/{pid}.png").write_bytes(b"png")
        run = {**RUN1, "build": "1.43", "cases": [{"id": c["id"], "status": "PASS"} for c in plan["cases"]]}
        d, r = self.render_run([("form", run)], plan=plan)
        row = next(x for x in r.splitlines() if x.startswith("| PROJ-2"))
        self.assertIn(f"0 / 0 / 0 (+{len(plan['prior_passed'])} prior)", row)
        draft = (d / "jira-comment-PROJ-2.md").read_text()
        self.assertNotIn("unknown in this run", draft)
        self.assertNotIn("No screenshot", draft)
        self.assertIn(f"2026-11-23-PROJ-1+PROJ-2/evidence/{pid}.png", draft)

    def test_partial_retest_verdict_is_pass_with_notes_and_honest_draft(self):
        plan = {**PLAN1, "retest_of": "2026-11-23-PROJ-1",
                "cases": [PLAN1["cases"][1]],  # only TC-2 retested
                "prior_passed": [{**PLAN1["cases"][0], "build": "1.42"}]}
        run_pass = {**RUN1, "build": "1.43", "cases": [{"id": "TC-2", "status": "PASS", "actual": "Error shown"}]}
        d, r = self.render_run([("form", run_pass)], plan=plan)
        self.assertIn("retest of 2026-11-23-PROJ-1", r)
        self.assertIn("**Verdict: ⚠️ PASS WITH NOTES**", r)
        self.assertIn("verified on prior build 1.42; not re-run here", r)
        self.assertIn("- AC-1 Saves 0-130 — verified on prior build 1.42 (TC-1)", r)
        self.assertIn("<summary>Prior-build passes not re-run here (1)</summary>", r)
        draft = (d / "jira-comment-PROJ-1.md").read_text()
        self.assertIn("Partial retest of 2026-11-23-PROJ-1", draft)
        self.assertIn("TC-1 - PASS (prior build 1.42): Save 130", draft)
        self.assertIn("TC-2 - PASS: Reject 131", draft)
        # If the retested case fails, verdict is FAIL with no draft
        run_fail = {**RUN1, "build": "1.43", "cases": [{"id": "TC-2", "status": "FAIL", "actual": "Still saves 131"}]}
        d2, r2 = self.render_run([("form", run_fail)], plan=plan)
        self.assertIn("**Verdict: ❌ FAIL**", r2)
        self.assertIn("QA: FAIL", (d2 / "jira-comment-PROJ-1.md").read_text())


class JiraBundles(Rendering):
    """Jira comment drafts for every verdict, with a clean body file; filed bugs get a comment, not a new bug."""

    def test_a_fail_gets_a_rejection_draft_with_a_clean_body_and_a_printed_command(self):
        run = {**RUN1, "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": "FAIL", "actual": "131 saved",
                                                                       "bug": "BUG-1"}],
               "bugs": [{"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved", "severity": "HIGH",
                         "classification": "CONFIRMED", "draft_path": "bugs/BUG-1.md"}]}
        d, r = self.render_run([("form", run)], plan=PLAN1_ANSWERED)
        md, txt = (d / "jira-comment-PROJ-1.md").read_text(), (d / "jira-comment-PROJ-1.txt").read_text()
        self.assertIn("CHECK BEFORE POSTING", md)
        self.assertNotIn("CHECK BEFORE POSTING", txt)
        self.assertIn("QA: FAIL on build 1.42", txt)
        self.assertIn("TC-2 - FAIL (AC-2): 131 saved", txt)
        self.assertIn("BUG-1", txt)
        self.assertIn(f"acli jira workitem comment create --key PROJ-1 --body-file {d / 'jira-comment-PROJ-1.txt'}", r)
        run_ok = {**RUN1}
        d2, _ = self.render_run([("form", run_ok)], plan=PLAN1_ANSWERED)
        self.assertIn("Passed on", (d2 / "jira-comment-PROJ-1.txt").read_text())  # pass drafts get the clean body too

    def test_a_bug_already_filed_gets_a_comment_and_a_new_one_a_link_when_configured(self):
        from qa_sdet import memory
        con = core.connect()
        memory.ingest(con, {**RUN1, "run_id": "old", "bugs": [{"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved"}]})
        h = core.bug_hash("/form", "131 saved")
        con.execute("UPDATE bugs SET status='REGRESSED', jira_key='PROJ-55' WHERE hash=?", (h,))
        con.commit()
        con.close()
        bugs = [{"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved", "hash": h, "severity": "HIGH",
                 "classification": "CONFIRMED", "draft_path": "bugs/BUG-1.md"},
                {"route": "/form", "title": "Name accepts 300 chars", "symptom": "300 chars", "severity": "LOW",
                 "classification": "CONFIRMED", "draft_path": "bugs/BUG-2.md", "ticket": "PROJ-1"}]
        self.config(releaseTemplates=TEMPLATES, jiraBugLinkType="Blocks")
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1_ANSWERED, runs=[("form", {**RUN1, "bugs": bugs})])
        for i in (1, 2):
            (d / f"bugs/BUG-{i}.md").write_text(f"# t{i}\n")
        (d / ".data/jira-dups.json").write_text(json.dumps({"BUG-2": [{"key": "PROJ-77", "summary": "Name too long saved"}]}))
        report.write_report(d)
        block = (d / "REPORT.md").read_text().split("Filing commands")[1]
        one, two = block.split("**BUG-2**")
        self.assertIn("already in Jira as PROJ-55 (REGRESSED)", one)
        self.assertIn(f"acli jira workitem comment create --key PROJ-55 --body-file {d / 'bugs/BUG-1.md'}", one)
        self.assertNotIn("workitem create --project", one)
        self.assertIn("acli jira workitem link create --out <JIRA-KEY> --in PROJ-1 --type Blocks --yes", two)
        self.assertIn("possible duplicate: PROJ-77 Name too long saved", two)
        self.config(releaseTemplates=TEMPLATES)  # no jiraBugLinkType: no link command
        report.write_report(d)
        self.assertNotIn("link create", (d / "REPORT.md").read_text())

    def test_dup_check_saves_the_search_hits_for_the_report(self):
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1_ANSWERED, runs=[("form", RUN1)])
        hits = json.dumps([{"key": "PROJ-77", "fields": {"summary": "Name too long saved"}}])
        self.assertEqual(cli("dup-check", str(d), "BUG-2", stdin=hits)[0], 0)
        self.assertEqual(json.loads((d / ".data/jira-dups.json").read_text()),
                         {"BUG-2": [{"key": "PROJ-77", "summary": "Name too long saved"}]})


class FilingBundle(Rendering):
    """Persistent shell-safe filing commands & attachments folded inside REPORT.md."""

    def test_confirmed_draft_gets_safe_filing_block_and_suspected_does_not(self):
        import subprocess
        self.config(jiraProject="DEMO")
        title = "Can't save `id` and $(echo INJECTED) at 131"
        bugs = [
            {"route": "/form", "title": title, "symptom": "131 saved", "severity": "HIGH",
             "classification": "CONFIRMED", "draft_path": "bugs/BUG-1.md", "ticket": "PROJ-1"},
            {"route": "/form", "title": "Flaky toast", "symptom": "toast", "severity": "LOW",
             "classification": "SUSPECTED", "draft_path": "bugs/BUG-2.md"},
        ]
        d = self.rundir("2026-11-24-PROJ-1", plan=PLAN1, runs=[("form", {**RUN1, "bugs": bugs})])
        (d / "bugs/BUG-1.md").write_text(f"# {title}\n\nbody\n")
        (d / "bugs/BUG-2.md").write_text("# Flaky toast\n\nbody\n")
        (d / "evidence/BUG-1.png").write_bytes(b"png")
        (d / "evidence/BUG-1.webm").write_bytes(b"webm")
        (d / "repro/BUG-1.spec.ts").write_text("test('x', () => {});")
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("<summary>Filing commands &amp; attachments (1 confirmed)</summary>", r)
        self.assertNotIn("Flaky toast\n", r.split("Filing commands", 1)[1])
        h = core.bug_hash("/form", "131 saved")
        self.assertIn(f"qa-mem bug-status {h} FILED <JIRA-KEY>", r)
        self.assertIn("BUG-1.png", r.split("Filing commands", 1)[1])
        self.assertIn("BUG-1.webm", r.split("Filing commands", 1)[1])
        self.assertIn("BUG-1.spec.ts", r.split("Filing commands", 1)[1])
        # Verify the printed acli command is shell-safe (does not execute $(echo INJECTED) or backticks)
        cmd = next(ln for ln in r.splitlines() if ln.startswith("acli jira workitem create "))
        out = subprocess.run(["bash", "-c", cmd.replace("acli ", "printf '%s\\n' ", 1)], capture_output=True, text=True).stdout
        self.assertNotIn("INJECTED\n", out)
        self.assertIn("$(echo INJECTED)", out)
        # Draft file itself stays clean (no filing command injected into --description-file)
        self.assertNotIn("acli jira", (d / "bugs/BUG-1.md").read_text())

    def test_a_blank_symptom_hashes_the_title_like_ingest(self):
        bug = {"route": "/form", "title": "Speed accepts 999", "symptom": "   ", "severity": "HIGH",
               "classification": "CONFIRMED", "draft_path": "bugs/BUG-1.md"}
        d = self.rundir("2026-11-24-PROJ-1", plan=PLAN1, runs=[("form", {**RUN1, "bugs": [bug]})])
        (d / "bugs/BUG-1.md").write_text("# Speed accepts 999\n")
        report.write_report(d)
        self.assertIn(f"qa-mem bug-status {core.bug_hash('/form', 'Speed accepts 999')} FILED", (d / "REPORT.md").read_text())

    def test_a_resighted_bugs_follow_up_names_the_hash_memory_stores(self):  # a reworded regression named no bug
        con = core.connect()
        try:
            memory.ingest(con, {"run_id": "r-old", "target": "/form", "build": "1.40", "bugs": [
                {"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved"}]}, "2026-11-01T00:00:00Z")
            h = core.bug_hash("/form", "131 saved")
            con.execute("UPDATE bugs SET status='FIXED' WHERE hash=?", (h,))
            con.commit()
        finally:
            con.close()
        back = {"route": "/form", "title": "Speed accepts 131 again", "symptom": "131 is saved once more", "hash": h,
                "severity": "HIGH", "classification": "CONFIRMED", "draft_path": "bugs/BUG-1.md"}
        jira = {**back, "title": "Known Jira bug", "symptom": "x", "hash": "CAFE-12", "draft_path": "bugs/BUG-2.md"}
        d = self.rundir("2026-11-24-PROJ-1", plan=PLAN1, runs=[("form", {**RUN1, "run_id": "r-new", "bugs": [back, jira]})])
        for n in (1, 2):
            (d / f"bugs/BUG-{n}.md").write_text(f"# Draft {n}\n")
        report.write_report(d)
        follow = re.findall(r"qa-mem bug-status (\S+) FILED", (d / "REPORT.md").read_text())
        self.assertEqual(follow, [h, core.bug_hash("/form", "x")])  # a Jira key is no hash: not 'cafe'
        self.assertEqual(cli("ingest", str(d / ".data/run-form.json"))[:2], (0, "ingested\n"))  # what the hook stores
        code, out, _ = cli("bug-status", follow[0], "FILED", "PROJ-9")
        self.assertEqual((code, out.strip()), (0, f"updated {h} -> FILED (PROJ-9)"))


GOOD_SPEC = """import { test, expect } from '@playwright/test';
test.use({ baseURL: process.env.BASE_URL ?? 'https://qa.example',
  ...(process.env.STORAGE_STATE ? { storageState: process.env.STORAGE_STATE } : {}) });
test('speed above 130 is rejected', async ({ page }) => {
  await page.goto('/form');
  await page.getByLabel('Speed limit').fill('131');
  await expect(page.getByRole('status')).toHaveText(/between 0 and 130/);
});
"""


def good_draft(h, **change):
    d = {"title": "# Speed accepts 131", "cls": "CONFIRMED", "repro": "2/2", "hash": h,
         "step1": "1. Open https://qa.example/form", "oracle": " — **Oracle:** AC-2"}
    d.update(change)
    return (f"{d['title']}\n\n**Classification:** {d['cls']} · **Severity:** HIGH · **Route:** /form\n"
            f"**Ticket:** PROJ-1 · **Repro:** {d['repro']} · **qa-mem hash:** {d['hash']}\n\n## Steps\n{d['step1']}\n2. Type 131\n\n"
            f"## Expected\nAn error{d['oracle']}\n\n## Actual\nSaved\n\n## Impact\nWrong data.\n\n## Evidence\n- Screenshot\n")


class AuditBugs(Rendering):
    """`qa-mem audit-bugs <run>`: bug drafts and repro specs checked against references/bug-draft.md and the run JSON."""

    H = core.bug_hash("/form", "131 saved")

    def folder(self, draft, spec, png=True, name="2026-11-25-PROJ-1", **bug):
        b = {"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved", "severity": "HIGH",
             "classification": "CONFIRMED", "draft_path": "bugs/BUG-1.md", **bug}
        d = self.rundir(name, plan=PLAN1_ANSWERED, runs=[("form", {**RUN1, "bugs": [b]})])
        for path, body in (("bugs/BUG-1.md", draft), ("repro/BUG-1.spec.ts", spec)):
            if body is not None:
                (d / path).write_text(body)
        if png:
            (d / "evidence/BUG-1.png").write_bytes(b"png")
        return d

    def issues(self, d):
        return [(crit, text) for crit, text in report.audit_bugs(d).get("BUG-1", [])]

    def test_a_complete_draft_and_spec_pass_and_the_report_says_so(self):
        d = self.folder(good_draft(self.H), GOOD_SPEC)
        self.assertEqual(report.audit_bugs(d), {})
        report.write_report(d)
        self.assertIn("repro ✓", (d / "REPORT.md").read_text())

    def test_what_makes_a_repro_useless_is_critical(self):
        for n, (draft, spec, want) in enumerate(((good_draft(self.H), GOOD_SPEC.replace("await expect(", "await ("), "no expect("),
                                                 (good_draft("0123456789ab"), GOOD_SPEC, "hash"),
                                                 (good_draft(self.H), None, "no repro spec"),
                                                 (None, GOOD_SPEC, "no draft"))):
            d = self.folder(draft, spec, name=f"2026-11-25-case-{n}")
            crit = [t for c, t in self.issues(d) if c]
            self.assertTrue(any(want in t for t in crit), (want, self.issues(d)))
            report.write_report(d)
            self.assertIn("⚠", (d / "REPORT.md").read_text().split("### Bugs to file")[1].split("\n\n")[0])
        code, out, _ = cli("audit-bugs", str(d))
        self.assertEqual(code, 0)
        self.assertIn("BUG-1", out)

    def test_a_review_shows_next_to_its_bug_and_never_changes_the_verdict(self):
        d = self.folder(good_draft(self.H), GOOD_SPEC)
        review = {"bugs": [{"id": "BUG-1", "verdict": "needs_check", "oracle_ok": False, "notes": "the oracle cites the code"}]}
        (d / ".data/review.json").write_text(json.dumps(review))
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("⚠ check before filing: the oracle cites the code", r)
        self.assertIn("**Verdict: ❌ FAIL**", r)  # the review is a note for the user, not a verdict
        (d / ".data/review.json").write_text(json.dumps({"bugs": [{"id": "BUG-1", "verdict": "solid"}]}))
        self.assertIn("reviewed ✓", report.render_report(d))
        (d / ".data/review.json").write_text("{oops")
        self.assertIn("review.json unreadable", report.render_report(d))

    def test_network_and_repro_are_linked_and_files_are_scrubbed_before_the_report(self):
        d = self.folder(good_draft(self.H) + "Authorization: Bearer abcdefghijklmnopqrst\n", GOOD_SPEC)
        (d / "evidence/BUG-1-network.txt").write_text("POST /api/zones => 500\n{\"code\": \"ZONE_OVERLAP\"}\n")
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        row = next(x for x in r.splitlines() if x.startswith("| [BUG-1]"))
        self.assertIn("[repro](repro/BUG-1.spec.ts)", row)
        self.assertIn("[network](evidence/BUG-1-network.txt)", row)
        self.assertIn("BUG-1-network.txt`", r.split("Filing commands")[1])  # attached to the Jira bug too
        self.assertNotIn("abcdefghijklmnopqrst", (d / "bugs/BUG-1.md").read_text())  # scrubbed before it was linked

    def test_a_checked_case_without_its_image_is_an_evidence_gap(self):
        d = self.rundir("2026-11-25-gap", plan=PLAN1_ANSWERED, runs=[("form", RUN1)])
        (d / "evidence/TC-1.png").write_bytes(b"png")
        (d / "evidence/TC-2.png").write_bytes(b"")  # 0 bytes: no image
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("### Evidence gaps (1)\n- TC-2: no evidence image (evidence/TC-2.png)", r)
        self.assertIn("TC-2 has no evidence image", (d / "jira-comment-PROJ-1.md").read_text())

    def test_contract_warnings(self):
        spec = GOOD_SPEC.replace("'/form'", "'/incidents/:id'").replace("...(process.env.STORAGE_STATE ? { storageState: "
                                                                          "process.env.STORAGE_STATE } : {})", "") \
            + "await page.waitForTimeout(500); await page.goto('https://qa.example/x');\n"
        draft = good_draft(self.H, repro="1/2", step1="1. Open /form", oracle="", title="#")
        warn = [t for c, t in self.issues(self.folder(draft, spec, png=False)) if not c]
        for want in (":id", "waitForTimeout", "absolute URL", "STORAGE_STATE", "Step 1", "Oracle", "Repro 1/", "title",
                     "evidence image"):
            self.assertTrue(any(want in t for t in warn), (want, warn))
        self.assertEqual(report.spec_issues(GOOD_SPEC), [])
        self.assertEqual(report.draft_issues(good_draft(self.H)), [])


if __name__ == "__main__":
    unittest.main()
