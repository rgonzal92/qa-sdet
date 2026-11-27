"""What a run folder's files say (qa_sdet.results): the verdict, merged cases, and earlier results a re-run keeps."""

import json
import time
import unittest

from support import PLAN1, PLAN1_ANSWERED, RUN1, Base, Rendering

from qa_sdet import report, results, runstate


class Verdict(unittest.TestCase):
    """results.verdict: PASS, PASS WITH NOTES, FAIL or BLOCKED for one ticket, and which bugs count as its own."""

    def case(self, cid, status, ac="AC-1", route="/form", **kw):
        return {"id": cid, "ticket": "T-1", "ac": ac, "route": route, "status": status, "notes": "", "actual": "", **kw}

    def test_four_outcomes(self):
        v = results.verdict
        self.assertEqual(
            v("T-1", [self.case("TC-1", "PASS"), self.case("TC-2", "PASS")], []), ("PASS", "all 2 cases passed")
        )
        for missing in (None, ""):  # a case with no status was not run: BLOCKED, never a pass (mutation testing)
            self.assertEqual(v("T-1", [self.case("TC-1", "PASS"), self.case("TC-2", missing)], [])[0], "BLOCKED")
        self.assertEqual(
            v(
                {"key": "T-1"},
                [self.case("TC-1", "PASS"), self.case("TC-3", "FAIL", "AC-2")],
                [{"id": "BUG-1", "ticket": "T-1", "severity": "HIGH", "classification": "CONFIRMED"}],
            ),
            ("FAIL", "TC-3 failed (AC-2); BUG-1 HIGH"),
        )
        self.assertEqual(
            v(
                "T-1",
                [self.case("TC-1", "PASS")],
                [{"id": "BUG-4", "route": "/form", "severity": "CRITICAL", "classification": "CONFIRMED"}],
            )[0],
            "FAIL",
        )  # untagged bug on one of its routes
        self.assertEqual(
            v("T-1", [self.case("TC-1", "PASS"), self.case("TC-5", "NOT_RUN", notes="session expired")], []),
            ("BLOCKED", "TC-5 not run"),  # what it said is under its AC in REPORT.md, once
        )
        self.assertEqual(v("T-1", [self.case("TC-5", "BLOCKED")], [])[0], "BLOCKED")
        same = [self.case(f"TC-{i}", "BLOCKED", notes="browser can't start") for i in range(1, 13)]
        self.assertEqual(v("T-1", same, []), ("BLOCKED", "12 cases (TC-1–TC-12) blocked"))
        self.assertEqual(v("T-1", [self.case("TC-1", "PASS", ticket="T-2")], []), ("BLOCKED", "no test cases"))
        self.assertEqual(v("T-1", [self.case("TC-1", "QUESTION")], []), ("PASS WITH NOTES", "TC-1 open question"))
        self.assertEqual(
            v(
                "T-1",
                [self.case("TC-1", "PASS", bug="BUG-2")],
                [{"id": "BUG-2", "route": "/other", "severity": "HIGH", "classification": "SUSPECTED"}],
            ),
            ("PASS WITH NOTES", "BUG-2 HIGH suspected"),
        )  # referenced by a case; suspected isn't FAIL
        self.assertEqual(
            v(
                "T-1",
                [self.case("TC-1", "PASS")],
                [
                    {
                        "id": "BUG-9",
                        "route": "/other",
                        "ticket": "T-2",
                        "severity": "HIGH",
                        "classification": "CONFIRMED",
                    }
                ],
            )[0],
            "PASS",
        )  # another ticket's bug

    def test_an_open_blocking_question_keeps_its_ticket_from_a_plain_pass(self):  # all cases passed, the rule undecided
        cases = [self.case("TC-1", "PASS"), self.case("TC-2", "PASS", ticket="T-2", route="/list")]
        v = lambda key, *qs, cs=cases: results.verdict(
            key, cs, [], known=["T-1", "T-2"], questions=results.question_items({"questions": list(qs)}, "test plan")
        )
        both = "Q1 [BLOCKING] Is 130 itself allowed?"  # untagged (a string can't name a ticket): every ticket's
        self.assertEqual([v(k, both) for k in ("T-1", "T-2")], [("PASS WITH NOTES", "Q1 open blocking question")] * 2)
        mine = {"text": "Q2 [BLOCKING] Which role may edit?", "ticket": " t-2"}
        self.assertEqual(
            (v("T-1", mine)[0], v("T-2", mine)), ("PASS", ("PASS WITH NOTES", "Q2 open blocking question"))
        )
        unknown = {**mine, "ticket": "OTHER-9"}  # a key outside the plan is no tag, as for bugs
        self.assertEqual(v("T-1", unknown)[0], "PASS WITH NOTES")
        for not_blocking in ({"text": both, "status": "Resolved", "answer": "Yes"}, "Q3 [HIGH] Units?", "Q4 [LOW] x"):
            self.assertEqual(v("T-1", not_blocking)[0], "PASS", not_blocking)
        self.assertEqual(
            v("T-1", "[blocking] no number here"),
            ("PASS WITH NOTES", "1 open blocking question"),  # its text is in What to do
        )
        # it is named first, so the pass draft's Notes line never cuts it off
        busy = [self.case(f"TC-{i}", "QUESTION") for i in range(1, 5)]
        self.assertTrue(v("T-1", both, cs=busy)[1].startswith("Q1 open blocking question; TC-1 open question"))
        self.assertEqual(v("T-1", both, cs=[self.case("TC-1", "FAIL")])[0], "FAIL")  # worse verdicts still win
        self.assertEqual(
            results.verdict(
                "T-1", cases, [], started=False, questions=results.question_items({"questions": [both]}, "")
            )[0],
            "NOT STARTED",
        )

    def test_a_bug_named_only_by_a_case_of_no_ticket_still_counts_on_its_route(self):  # it was nobody's: a false PASS
        bug = [{"id": "BUG-1", "route": "/form", "severity": "HIGH", "classification": "CONFIRMED"}]
        own = self.case("TC-9", "FAIL", ticket=None, bug="BUG-1", actual="two zones saved")  # an explorer's own case
        for known in ((), ("T-1", "T-2")):
            got = results.verdict("T-1", [self.case("TC-1", "PASS"), own], bug, known=known)
            self.assertEqual(got[0], "FAIL", known)
            self.assertIn("BUG-1 HIGH", got[1])

    def test_known_bug_named_by_hash_belongs_to_that_ticket_only(self):
        cases = [
            {"id": "TC-1", "ticket": "A-1", "route": "/list", "status": "PASS"},
            {"id": "TC-2", "ticket": "B-1", "route": "/list", "status": "PASS", "bug": "known 0123456789ab"},
        ]
        for raw in ("0123456789abcdef", "#0123456789ab", "0123456789ab (known)"):
            known = [{"hash": raw, "route": "/list", "severity": "HIGH", "classification": "CONFIRMED", "title": "x"}]
            self.assertEqual(results.verdict("A-1", cases, known)[0], "PASS", raw)
            self.assertEqual(results.verdict("B-1", cases, known)[0], "FAIL", raw)

    def test_multi_ticket_runs(self):
        v = results.verdict
        # shared /form: an untagged bug named by T-2's failing case is T-2's, not T-1's
        cases = [self.case("TC-1", "PASS"), self.case("TC-2", "FAIL", ticket="T-2", bug="BUG-1", actual="131 saved")]
        bug = [{"id": "BUG-1", "route": "/form", "severity": "HIGH", "classification": "CONFIRMED"}]
        self.assertEqual(v("T-1", cases, bug), ("PASS", "all 1 cases passed"))
        self.assertEqual(v("T-2", cases, bug), ("FAIL", "TC-2 failed (AC-1); BUG-1 HIGH"))
        # a case covering two AC covers both; an AC without a case is a note, not a PASS
        t = {"key": "T-1", "acs": [{"id": "AC-1"}, {"id": "AC-2"}, {"id": "AC-3", "text": "x"}]}
        two = [self.case("TC-1", "PASS", ac="AC-1, AC-2")]
        self.assertEqual(v(t, two, []), ("PASS WITH NOTES", "AC-3 has no test case"))
        blocks = report._results(t, two, "/x", True, 3)
        self.assertEqual(
            [b for b in blocks if b.startswith("#")],
            ["### AC-1: PASS", "### AC-2: PASS", "### AC-3 x: not covered by a case"],
        )
        self.assertEqual(blocks[3], "- See TC-1 under AC-1.")  # listed once, under its first AC
        # a route skipped after another explorer was BLOCKED: say which route has no result
        self.assertEqual(
            v("T-1", [self.case("TC-1", "NOT_RUN", route="/list")], []),
            ("BLOCKED", "TC-1 not run: no result reported for /list"),
        )
        # a re-sighted known bug (hash, no draft) says so
        self.assertEqual(
            v(
                "T-1",
                [self.case("TC-1", "PASS")],
                [
                    {
                        "hash": "abc123abc123",
                        "route": "/form",
                        "title": "Save 500",
                        "severity": "HIGH",
                        "classification": "CONFIRMED",
                    }
                ],
            ),
            ("FAIL", "known bug Save 500 HIGH"),
        )

    def test_several_failures_named_and_never_quoted_cut(self):  # "TC-3 failed (AC-1): … at 1…; …; +4 more"
        acts = [
            "Speed 131 shows 'Saved zone \"qa-sdet-zone-c\" at 131 km/h', no error",
            "55.5 saved",
            "blank saved as 0",
            "1000 saved",
        ]
        cases = [self.case(f"TC-{i}", "FAIL", ac="AC-1" if i < 4 else "AC-10", actual=a) for i, a in enumerate(acts, 1)]
        bugs = [
            {"id": f"BUG-{i}", "ticket": "T-1", "severity": "HIGH", "classification": "CONFIRMED"} for i in (1, 2, 3)
        ]
        self.assertEqual(
            results.verdict("T-1", cases, bugs),
            ("FAIL", "4 cases failed (TC-1, TC-2, TC-3, TC-4) on AC-1, AC-10; BUG-1 HIGH; BUG-2 HIGH; +1 more"),
        )
        v, why = results.verdict("T-1", cases[:1], [])
        self.assertEqual(why, "TC-1 failed (AC-1)")  # what it saw is under its AC, whole
        stuck = self.case(
            "TC-12", "NOT_RUN", actual="Not testable: faults are off", notes="The 0 seen comes from the 404"
        )
        self.assertEqual(results.verdict("T-1", [stuck], [])[1], "TC-12 not run")
        item = report._case_item({**stuck, "title": "t"}, "/x", True)
        self.assertTrue(
            item.startswith("- **TC-12 t: NOT RUN.** Not testable: faults are off. Notes: The 0 seen"), item
        )
        nine = [self.case(f"TC-{i}", "FAIL") for i in range(1, 10)]
        self.assertEqual(
            results.verdict("T-1", nine, [])[1], "9 cases failed (TC-1, TC-2, TC-3, TC-4, TC-5, TC-6, +3) on AC-1"
        )

    def test_untagged_or_mistagged_serious_bug_still_fails_the_ticket(self):
        case = {"id": "TC-1", "ticket": "S-1", "route": "/incidents", "status": "PASS", "notes": "", "actual": ""}
        serious = {"severity": "CRITICAL", "classification": "CONFIRMED", "id": "BUG-1"}
        for bug in (
            {**serious, "route": "/incidents/:id", "target": "/incidents"},  # a record opened from the list page
            {**serious, "route": "/incidents", "ticket": "none"},
            {**serious, "route": "/incidents", "ticket": "s-1 "},
        ):
            self.assertEqual(results.verdict("S-1", [case], [bug])[0], "FAIL", bug)
        other = {**serious, "route": "/elsewhere", "target": "/elsewhere"}
        self.assertEqual(results.verdict("S-1", [case], [other])[0], "PASS")
        self.assertEqual(
            results.run_bugs([("a", {"target": "/incidents", "bugs": [{"route": "/incidents/4521", "title": "t"}]})])[
                0
            ]["target"],
            "/incidents",
        )

    def test_bug_tagged_with_a_plan_ticket_that_has_no_cases_stays_with_it(self):
        case = lambda cid, t: {"id": cid, "ticket": t, "route": "/form", "status": "PASS", "notes": "", "actual": ""}
        cases = [case("TC-1", "S-1"), case("TC-3", "S-2")]
        bug = {"id": "BUG-1", "ticket": "S-3", "route": "/form", "severity": "HIGH", "classification": "CONFIRMED"}
        keys = ["S-1", "S-2", "S-3"]
        self.assertEqual([results.verdict(k, cases, [bug], True, keys)[0] for k in keys], ["PASS", "PASS", "FAIL"])

    def test_a_run_that_ended_blocked_blocks_its_tickets(self):  # its PASS results can't be trusted
        cases = [self.case("TC-1", "PASS")]
        blocked = {
            "target": "/form",
            "status": "Blocked",
            "summary": "session expired, run /qa-sdet:qa-login",
            "cases": [{"id": "TC-1", "status": "PASS"}],
        }
        self.assertEqual(
            results.verdict("T-1", cases, [], runs=[("form", blocked)]),
            ("BLOCKED", "the run on /form ended BLOCKED: session expired, run /qa-sdet:qa-login"),
        )
        by_id = {**blocked, "target": "https://qa.example/elsewhere"}  # it reported the ticket's case
        self.assertEqual(results.verdict("T-1", cases, [], runs=[("x", by_id)])[0], "BLOCKED")
        other = {"target": "/list", "status": "BLOCKED", "cases": []}  # another ticket's page
        self.assertEqual(results.verdict("T-1", cases, [], runs=[("list", other)])[0], "PASS")
        incomplete = {**blocked, "status": "INCOMPLETE"}  # budget ran out after the cases: they stand
        self.assertEqual(results.verdict("T-1", cases, [], runs=[("form", incomplete)])[0], "PASS")

    def test_severity_and_classification_read_by_their_first_word(self):  # "Confirmed (2/2)" was no CONFIRMED
        cases = [self.case("TC-1", "PASS")]
        bug = {"id": "BUG-1", "route": "/form", "severity": "High ", "classification": "Confirmed (2/2)"}
        self.assertEqual(results.verdict("T-1", cases, [bug]), ("FAIL", "BUG-1 HIGH"))  # no second class in the reason
        self.assertEqual(results.verdict("T-1", cases, [{**bug, "severity": "critical - data loss"}])[0], "FAIL")
        for weak in (
            {**bug, "classification": "Not confirmed"},
            {**bug, "classification": "SUSPECTED"},
            {**bug, "severity": "MEDIUM (high impact later)"},
        ):
            self.assertEqual(results.verdict("T-1", cases, [weak])[0], "PASS WITH NOTES", weak)


class MergedCases(Base):
    """Several runs' results for one case become one."""

    def test_most_decisive_result_wins(self):
        plan = {
            "kind": "ticket",
            "tickets": [{"key": "PROJ-1"}],
            "cases": [
                {"id": "TC-1", "ticket": "PROJ-1", "route": "/a"},
                {"id": "TC-2", "ticket": "PROJ-1", "route": "/b"},
                {"id": "TC-3", "ticket": "PROJ-1", "route": "/b"},
            ],
        }
        older = {
            "target": "/a",
            "cases": [
                {"id": "TC-1", "status": "FAIL", "actual": "131 saved"},
                {"id": "TC-2", "status": "NOT_RUN"},
                {"id": "TC-3", "status": "BLOCKED"},
            ],
        }
        newer = {
            "target": "/b",
            "cases": [
                {"id": "TC-2", "status": "PASS"},
                {"id": "TC-3", "status": "PASS"},
                {"id": "TC-1", "status": "NOT_RUN", "notes": "other route"},
            ],
        }
        d = results.load_run(self.rundir(plan=plan, runs=[("a", older), ("b", newer)]))
        cases = results.merged_cases(d["plan"], d["runs"])
        self.assertEqual(
            [(c["id"], c["status"], c["actual"]) for c in cases],
            [("TC-1", "FAIL", "131 saved"), ("TC-2", "PASS", ""), ("TC-3", "PASS", "")],
        )
        self.assertEqual(results.verdict("PROJ-1", cases, []), ("FAIL", "TC-1 failed"))
        # a PASS from another run never hides a FAIL or a QUESTION, whichever run is newer
        for worse in ("FAIL", "QUESTION"):
            for first, second in ((worse, "PASS"), ("PASS", worse)):
                two = [
                    ("a", {"cases": [{"id": "TC-1", "status": first}]}),
                    ("b", {"cases": [{"id": "TC-1", "status": second}]}),
                ]
                self.assertEqual(results.merged_cases(plan, two)[0]["status"], worse, (first, second))
        # same rank: the newest run wins
        tie = results.merged_cases(
            plan,
            [
                ("a", {"cases": [{"id": "TC-1", "status": "PASS", "actual": "old"}]}),
                ("b", {"cases": [{"id": "TC-1", "status": "pass", "actual": "new"}]}),
            ],
        )
        self.assertEqual(tie[0]["actual"], "new")

    def test_a_case_belongs_to_its_ticket_however_the_key_is_written(self):  # a FAIL outside the ticket hid it
        plan = {
            "kind": "ticket",
            "tickets": [{"key": "PROJ-1"}],
            "cases": [
                {"id": "TC-1", "ticket": "PROJ-1", "route": "/form"},
                {"id": "TC-2", "route": "/form"},
                {"id": "TC-3", "ticket": " proj-1", "route": "/form"},
            ],
        }
        runs = [
            (
                "form",
                {
                    "target": "/form",
                    "cases": [
                        {"id": "TC-1", "status": "PASS"},
                        {"id": "TC-2", "status": "FAIL"},
                        {"id": "TC-3", "status": "PASS"},
                    ],
                },
            )
        ]
        cases = results.merged_cases(plan, runs)
        self.assertEqual([c["ticket"] for c in cases], ["PROJ-1", "PROJ-1", "PROJ-1"])
        self.assertEqual(results.verdict("PROJ-1", cases, [])[0], "FAIL")
        two = {**plan, "tickets": [{"key": "PROJ-1"}, {"key": "PROJ-2"}]}  # two tickets: no guess for TC-2
        self.assertEqual([c.get("ticket") for c in results.merged_cases(two, runs)], ["PROJ-1", None, "PROJ-1"])
        only_run = results.merged_cases(plan, [("form", {"cases": [{"id": "TC-9", "status": "FAIL"}]})])
        self.assertIsNone(only_run[-1].get("ticket"))  # an explorer's own case is never assigned

    def test_a_float_case_id_ingest_accepts_is_reported(self):  # core.txt takes 1.0; the report dropped it silently
        cases = results.merged_cases({}, [("a", {"cases": [{"id": 1.0, "status": "FAIL", "actual": "x"}]})])
        self.assertEqual([(c["id"], c["status"]) for c in cases], [("1.0", "FAIL")])

    def test_case_without_or_with_a_repeated_id_is_named_not_dropped_silently(self):
        plan = {
            "kind": "ticket",
            "tickets": [{"key": "PROJ-1", "acs": [{"id": "AC-1"}, {"id": "AC-2"}]}],
            "cases": [
                {"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-1", "title": "Save 130"},
                {"id": "TC-1", "ticket": "PROJ-1", "ac": "AC-2", "title": "Reject 131"},  # added by hand in --review
                {"ticket": "PROJ-1", "ac": "AC-2", "title": "Reject 0"},
            ],
        }
        d = self.rundir(plan=plan)
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("## Problems reading this folder", r)
        self.assertIn(
            "- plan.json: case 'Reject 131' repeats id TC-1; it is left out of this report: give it an id "
            "of its own (e.g. TC-99)",
            r,
        )
        self.assertIn("- plan.json: case 'Reject 0' has no id;", r)

    def test_a_repeated_case_id_blocks_every_ticket_using_it(
        self,
    ):  # B-1's FAIL showed under A-1 and B-1 got a pass draft
        case = lambda cid, t, route: {"id": cid, "ticket": t, "ac": "AC-1", "title": f"{t} {cid}", "route": route}
        plan = {
            "kind": "ticket",
            "title": "A-1, B-1",
            "tickets": [
                {"key": "A-1", "acs": [{"id": "AC-1"}]},
                {"key": "B-1", "acs": [{"id": "AC-1"}]},
                {"key": "C-1", "acs": [{"id": "AC-1"}]},
            ],
            "cases": [
                case("TC-1", "A-1", "/a"),
                case("TC-1", "B-1", "/b"),
                case("TC-2", "B-1", "/b"),
                case("TC-3", "C-1", "/c"),
            ],
        }
        ran = lambda target, *cases: {
            "target": target,
            "build": "1",
            "status": "COMPLETED",
            "cases": [{"id": c, "status": s, "actual": "x"} for c, s in cases],
        }
        d = self.rundir(
            "2026-11-23-A-1+B-1",
            plan=plan,
            runs=[
                ("a", ran("/a", ("TC-1", "PASS"))),
                ("b", ran("/b", ("TC-1", "FAIL"), ("TC-2", "PASS"))),
                ("c", ran("/c", ("TC-3", "PASS"))),
            ],
        )
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        why = "TC-1 repeats an id in plan.json: its result can't be told apart"
        self.assertIn(f"- A-1: ⛔ BLOCKED — {why}\n", r)
        self.assertIn(f"- B-1: ⛔ BLOCKED — {why}\n", r)
        self.assertIn("- C-1: ✅ PASS — ", r)  # a ticket with ids of its own is untouched
        passes = sorted(p.name for p in d.glob("jira-comment-*.txt") if "QA result: PASSED" in p.read_text())
        self.assertEqual(passes, ["jira-comment-C-1.txt"])  # the others get a BLOCKED comment, never a pass
        self.assertIn(
            "A-1 ⛔ BLOCKED, B-1 ⛔ BLOCKED, C-1 ✅ PASS", (d.parent / "README.md").read_text()
        )  # index agrees
        # a real FAIL of the ticket still wins
        d2 = self.rundir(
            "2026-11-24-A-1+B-1",
            plan={**plan, "cases": plan["cases"] + [case("TC-4", "B-1", "/b")]},
            runs=[("b", ran("/b", ("TC-1", "PASS"), ("TC-2", "PASS"), ("TC-4", "FAIL")))],
        )
        report.write_report(d2)
        self.assertIn("- B-1: ❌ FAIL — TC-4 failed (AC-1)\n", (d2 / "REPORT.md").read_text())

    def test_numeric_id_zero_is_an_id(self):
        warnings = []
        cases = results.merged_cases(
            {"cases": [{"id": 0, "title": "zero"}]}, [("a", {"cases": [{"id": 0, "status": "PASS"}]})], warnings
        )
        self.assertEqual([(c["id"], c["status"]) for c in cases], [("0", "PASS")])
        self.assertEqual(warnings, [])


class CarryPrior(Rendering):
    """carry_prior: a route dispatched again keeps the earlier attempt's results and bugs in its run file."""

    PLAN = {**PLAN1_ANSWERED, "cases": [*PLAN1["cases"], {**PLAN1["cases"][1], "id": "TC-3", "title": "Reject -1"}]}
    BUG = lambda self, n, **kw: {"route": "/form", "title": f"bug {n}", "draft_path": f"bugs/BUG-{n}.md", **kw}
    KNOWN = {"route": "/form", "title": "known", "hash": "abc123def456"}

    def earlier(self, given, build="1.42", **run):
        """The run folder with the earlier attempt's run file and the prior copy dispatch saved; its run file."""
        d = self.rundir("2026-11-25-PROJ-1", plan=self.PLAN)
        old = {**RUN1, "run_id": "r1", "build": build, "status": "INCOMPLETE", **run}
        (d / ".data/run-form.json").write_text(json.dumps(old))
        prior = {"given": given, "ran": "2026-11-20", "run": old}
        (d / ".data/prior-form.json").write_text(json.dumps(prior))
        return d / ".data/run-form.json"

    def merged(self, f, **new):
        if new:
            f.write_text(json.dumps({**RUN1, "run_id": "r2", "status": "COMPLETED", **new}))
        results.carry_prior(f.parent.parent, "form")
        self.assertFalse((f.parent / "prior-form.json").exists())
        return json.loads(f.read_text())

    def test_an_earlier_bug_the_resumed_explorer_names_by_its_hash_is_one_bug(self):  # a live run counted it twice
        f = self.earlier(
            ["TC-1"],
            cases=[{"id": "TC-1", "status": "NOT_RUN"}, {"id": "TC-2", "status": "FAIL", "bug": "BUG-1"}],
            bugs=[self.BUG(1, title="Speed accepts 131", severity="HIGH")],
        )
        run = self.merged(
            f,
            cases=[{"id": "TC-1", "status": "PASS", "actual": "Saved"}],
            bugs=[{"route": "/form", "title": "Speed  accepts 131", "hash": "abc123def456"}, self.BUG(2)],
        )
        self.assertEqual(
            run["bugs"],
            [self.BUG(2), {**self.BUG(1, title="Speed accepts 131", severity="HIGH"), "hash": "abc123def456"}],
        )
        self.assertEqual(self.merged(f), run)

    def test_the_merge_keeps_a_result_and_a_bug_the_new_file_dropped(self):  # the bug a resumed explorer lost
        f = self.earlier(
            ["TC-3"],
            cases=[
                {"id": "TC-1", "status": "PASS", "actual": "Saved"},
                {"id": "TC-2", "status": "FAIL", "actual": "Saved 131", "bug": "BUG-1"},
                {"id": "TC-3", "status": "NOT_RUN", "notes": "time"},
            ],
            bugs=[self.BUG(1), self.BUG(3), self.KNOWN],  # BUG-3: no case names it, and the build is the same
            questions=["Units?", {"text": "Rounding?", "status": "open"}],
        )
        prior = (f.parent / "prior-form.json").read_text()
        run = self.merged(
            f,
            cases=[{"id": "TC-3", "status": "PASS", "actual": "Error shown"}],
            bugs=[{**self.KNOWN, "title": "known, again"}],
            questions=["Units?", "Max?"],
        )
        kept = {"build": "1.42", "ran": "2026-11-20"}
        self.assertEqual(
            run["cases"],
            [
                {"id": "TC-1", "status": "PASS", "actual": "Saved", **kept},
                {"id": "TC-2", "status": "FAIL", "actual": "Saved 131", "bug": "BUG-1", **kept},
                {"id": "TC-3", "status": "PASS", "actual": "Error shown"},
            ],
        )
        self.assertEqual([b["title"] for b in run["bugs"]], ["known, again", "bug 1", "bug 3"])  # the hash matched
        self.assertEqual(run["questions"], ["Units?", "Max?", {"text": "Rounding?", "status": "open"}])
        self.assertEqual((run["run_id"], run["status"]), ("r2", "COMPLETED"))  # the rest: the new file's
        before = f.read_bytes()
        self.assertEqual(self.merged(f), run)  # no prior copy: nothing to do
        (f.parent / "prior-form.json").write_text(prior)
        self.assertEqual(self.merged(f), run)  # the same copy merged twice: the same file
        self.assertEqual(f.read_bytes(), before)

    def test_a_given_case_takes_its_new_result_unless_it_was_not_run(self):
        f = self.earlier(
            ["TC-1", "TC-2", "TC-3"],
            cases=[
                {"id": "TC-1", "status": "PASS", "actual": "Saved"},
                {"id": "TC-2", "status": "PASS", "actual": "Error shown", "build": "1.40", "ran": "2026-11-01"},
                {"id": "TC-3", "status": "FAIL", "actual": "Saved -1", "bug": "BUG-1"},
            ],
            bugs=[self.BUG(1), self.BUG(3)],
        )
        run = self.merged(
            f,
            build="1.43",
            cases=[
                {"id": "TC-1", "status": "FAIL", "actual": "Saved 131", "bug": "BUG-2"},
                {"id": "TC-2", "status": "NOT_RUN", "notes": "time"},
                {"id": "TC-3", "status": "PASS", "actual": "Error shown"},
            ],
            bugs=[self.BUG(2)],
        )
        self.assertEqual(
            [(c["id"], c["status"], c.get("build"), c.get("ran")) for c in run["cases"]],
            [("TC-1", "FAIL", None, None), ("TC-2", "PASS", "1.40", "2026-11-01"), ("TC-3", "PASS", None, None)],
        )  # TC-2 keeps the build and date it first ran on
        # BUG-1: its only case ran again and passed; BUG-3: named by no case, on another build
        self.assertEqual([b["title"] for b in run["bugs"]], ["bug 2"])

    def test_no_new_run_file_leaves_the_earlier_one_as_it_is(self):  # the explorer crashed before writing
        f = self.earlier(["TC-2"], cases=[{"id": "TC-1", "status": "PASS", "actual": "Saved"}])
        before, when = f.read_bytes(), f.stat().st_mtime_ns
        self.merged(f)
        self.assertEqual((f.read_bytes(), f.stat().st_mtime_ns), (before, when))

    def test_the_report_merges_a_prior_copy_the_stop_hook_never_saw(self):
        f = self.earlier(["TC-2", "TC-3"], cases=[{"id": "TC-1", "status": "PASS", "actual": "Saved"}])
        f.write_text(json.dumps({**RUN1, "run_id": "r2", "cases": RUN1["cases"][1:]}))
        report.write_report(f.parent.parent)
        self.assertFalse((f.parent / "prior-form.json").exists())
        self.assertEqual([c["id"] for c in json.loads(f.read_text())["cases"]], ["TC-1", "TC-2"])

    def test_a_report_made_while_the_explorer_still_runs_keeps_the_copy(self):
        """`qa-mem report` in the middle of a page's run merges what is there, but the explorer will write its file
        again: without the copy, that last write would drop the earlier results after all."""
        f = self.earlier(["TC-2", "TC-3"], cases=[{"id": "TC-1", "status": "PASS", "actual": "Saved"}])
        d, copy = f.parent.parent, f.parent / "prior-form.json"
        marker = d.parent / ".active"
        marker.write_text(json.dumps({"expires": time.time() + 3600, "run_dir": str(d), "slug": "form"}))
        f.write_text(json.dumps({**RUN1, "run_id": "r2", "status": "INCOMPLETE", "cases": RUN1["cases"][1:]}))
        report.write_report(d)
        self.assertTrue(copy.exists())  # the run is live: the stop hook merges again
        f.write_text(json.dumps({**RUN1, "run_id": "r2", "cases": RUN1["cases"][1:]}))  # the explorer's last write
        marker.write_text(json.dumps({"expires": time.time() + 3600, "run_dir": str(d), "slug": "list"}))
        report.write_report(d)  # another page's run is live now: this one is over
        self.assertFalse(copy.exists())
        self.assertEqual([c["id"] for c in json.loads(f.read_text())["cases"]], ["TC-1", "TC-2"])

    def test_a_pass_kept_from_an_earlier_build_reads_like_a_plan_prior_pass(self):
        run = {**RUN1, "build": "1.43", "cases": [{**RUN1["cases"][0], "build": "1.42", "ran": "2026-11-20"}]}
        run["cases"].append(RUN1["cases"][1])
        d, r = self.render_run([("form", run)], plan=PLAN1_ANSWERED)
        draft = (d / "jira-comment.txt").read_text()
        st = runstate._run_state(d)  # a --retest of this folder carries TC-1 on the build it passed on
        self.assertEqual([(c["id"], c["build"]) for c in st["passed_cases"]], [("TC-1", "1.42"), ("TC-2", "1.43")])
        plan = {**PLAN1_ANSWERED, "cases": PLAN1["cases"][1:], "prior_passed": [{**PLAN1["cases"][0], "build": "1.42"}]}
        d2, r2 = self.render_run([("form", {**run, "cases": RUN1["cases"][1:]})], plan=plan)
        verdict = lambda text: text.split("\n\n")[1:3]  # the status line and why
        self.assertEqual(verdict(r), verdict(r2))
        self.assertEqual(
            verdict(r),
            [
                "⚠️ PASS WITH NOTES · build 1.43 · 2026-11-23 · 2 of 2 cases passed (1 on an earlier build) · no bugs",
                "1 case verified on prior build 1.42; not re-run here",
            ],
        )
        self.assertIn(
            "### AC-1 Saves 0-130: PASS on build 1.42\n\n- **TC-1 Save 130: PASS on build 1.42, not re-run here.** Saved",
            r,
        )
        self.assertEqual(r.count("**TC-1 "), 1)  # not also a current pass
        prior_entry = "\nAC-1 Saves 0-130: passed on build 1.42, not re-run on 1.43.\n"
        self.assertIn(prior_entry + "  Saved\n", draft)  # the kept result says what it saw
        self.assertIn(prior_entry + "  Save 130\n", (d2 / "jira-comment.txt").read_text())  # a plan's: its title

    def stopped_before_list(self, build="1.42"):
        """A ticket's folder keeps every re-run's files: the 1.43 re-run stopped after /form, /list last ran on `build`."""
        plan = {**PLAN1_ANSWERED, "cases": [PLAN1["cases"][0], {**PLAN1["cases"][1], "route": "/list"}]}
        older = {**RUN1, "target": "/list", "build": build, "cases": RUN1["cases"][1:]}
        return self.render_run([("list", older), ("form", {**RUN1, "build": "1.43", "cases": RUN1["cases"][:1]})], plan)

    def test_a_page_the_new_build_never_reached_holds_passes_not_re_run_here(self):  # found in audit: a false PASS
        d, r = self.stopped_before_list()
        self.assertEqual(
            r.split("\n\n")[1:3],
            [
                "⚠️ PASS WITH NOTES · build 1.43 · 2026-11-23 · 2 of 2 cases passed (1 on an earlier build) · no bugs",
                "1 case verified on prior build 1.42; not re-run here",
            ],
        )
        draft = (d / "jira-comment.txt").read_text()
        self.assertIn("QA result: PASSED WITH NOTES on build 1.43 (", draft)
        self.assertIn("\nAC-2 Rejects 131: passed on build 1.42, not re-run on 1.43.\n", draft)
        _, r = self.stopped_before_list("unknown")
        self.assertIn("\n\nall 2 cases passed\n\n", r)  # an unknown build can't be compared: What to do names it

    def test_a_page_the_new_build_never_reached_is_left_to_run(self):  # the same command said: already tested
        st = runstate._run_state(self.stopped_before_list()[0])
        self.assertEqual(
            (st["incomplete_routes"], st["completed_routes"], st["can_resume"]), (["/list"], ["/form"], True)
        )
        st = runstate._run_state(self.stopped_before_list("unknown")[0])
        self.assertEqual((st["incomplete_routes"], st["completed_routes"]), ([], ["/form", "/list"]))


if __name__ == "__main__":
    unittest.main()
