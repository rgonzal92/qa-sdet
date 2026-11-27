"""Where a run folder stands (qa_sdet.runstate): `plan-find`, --retest, a ticket's folder and `next-action`."""

import json
import os
import re
import time
import unittest
from pathlib import Path

from support import PLAN, PLAN1, PLAN1_ANSWERED, RUN1, RUN_FORM, Rendering, cli

from qa_sdet import core, inbox, report, results, runstate


class PlanFindAndRetest(Rendering):
    """`qa-mem plan-find` and honest partial-retest reporting (`retest_of` + `prior_passed`)."""

    def test_reusable_plan_includes_zero_execution_blocked_run(self):
        blocked = {
            "run_id": "t-1",
            "mode": "ticket",
            "target": "/form",
            "build": "unknown",
            "status": "BLOCKED",
            "summary": "session expired, run /qa-sdet:qa-login",
            "cases": [{"id": "TC-1", "status": "BLOCKED"}, {"id": "TC-2", "status": "NOT_RUN"}],
        }
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
        plan = {
            "kind": "ticket",
            "tickets": [{"key": "PROJ-1", "acs": [{"id": "AC-1"}]}, {"key": "PROJ-2"}],
            "cases": [
                case("TC-1", "PROJ-1", "/form"),
                case("TC-2", "PROJ-1", "/list"),
                case("TC-3", "PROJ-1", "/other"),
                case("TC-4", "PROJ-1", "/zones"),
                case("TC-5", "PROJ-1", "/fail"),
                case("TC-6", "PROJ-2", "/list"),
            ],
        }
        ran = lambda target, status, *cases, bugs=(): {
            "target": target,
            "build": "1.0",
            "status": status,
            "bugs": list(bugs),
            "cases": [{"id": c, "status": s} for c, s in cases],
        }
        bug = lambda sev: {"route": "/x", "title": f"{sev} bug", "severity": sev, "classification": "CONFIRMED"}
        prior = self.rundir(
            "2026-11-20-PROJ-1+PROJ-2",
            plan=plan,
            runs=[
                (
                    "form",
                    ran("/form", "BLOCKED", ("TC-1", "PASS")),
                ),  # session expired after TC-1: its PASS can't be trusted
                (
                    "list",
                    ran(
                        "/list",
                        "COMPLETED",
                        ("TC-2", "PASS"),
                        ("TC-6", "PASS"),
                        bugs=[{**bug("HIGH"), "route": "/list", "ticket": "PROJ-1"}],
                    ),
                ),  # PROJ-1's bug, not PROJ-2's
                ("other", ran("/other", "COMPLETED", ("TC-3", "PASS"), bugs=[{**bug("MEDIUM"), "route": "/other"}])),
                ("zones", ran("/zones", "INCOMPLETE", ("TC-4", "PASS"))),  # budget ran out after the cases: they stand
                ("fail", ran("/fail", "COMPLETED", ("TC-5", "FAIL"))),
            ],
        )
        report.write_report(prior)
        self.assertIn("\n- PROJ-1: ❌ FAIL — ", (prior / "REPORT.md").read_text())
        lr = json.loads(cli("plan-find", str(self.proj), "--run", str(prior))[1])["latest_run"]
        self.assertEqual([c["id"] for c in lr["retest_cases"]], ["TC-1", "TC-2", "TC-5"])
        self.assertEqual([c["id"] for c in lr["passed_cases"]], ["TC-3", "TC-4", "TC-6"])
        # the retest the skill builds from it: fixing TC-5 alone is no pass while TC-1 and TC-2 wait for a run
        retest = {
            **plan,
            "retest_of": prior.name,
            "cases": [c for c in plan["cases"] if c["id"] in ("TC-1", "TC-2", "TC-5")],
            "prior_passed": lr["passed_cases"],
        }
        d = self.rundir(
            "2026-11-21-PROJ-1+PROJ-2", plan=retest, runs=[("fail", ran("/fail", "COMPLETED", ("TC-5", "PASS")))]
        )
        report.write_report(d)
        self.assertIn("\n- PROJ-1: ⛔ BLOCKED — ", (d / "REPORT.md").read_text())
        self.assertIn("QA result: BLOCKED", (d / "jira-comment-PROJ-1.txt").read_text())

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
                plan = {
                    "kind": "ticket",
                    "tickets": [{"key": "PROJ-1"}],
                    "cases": [{"id": "TC-1", "ticket": "PROJ-1", "title": "t", "route": route}],
                }
                ok = {"target": route, "status": "COMPLETED", "cases": [{"id": "TC-1", "status": "PASS"}]}
                d = self.rundir("2026-11-20-PROJ-1", plan=plan, runs=[("a", ok), ("b", run)])
                cases = results.merged_cases(plan, results.load_run(d)["runs"])
                v = results.verdict({"key": "PROJ-1"}, cases, [], runs=results.load_run(d)["runs"])[0]
                retested = [c["id"] for c in runstate._run_state(d)["retest_cases"]]
                self.assertEqual((v, retested), ("BLOCKED", ["TC-1"]) if v == "BLOCKED" else ("PASS", []))

    def test_report_index_and_inbox_give_the_same_verdicts(self):  # one place feeds verdict(), not three
        case = lambda cid, t, route: {"id": cid, "ticket": t, "ac": "AC-1", "title": cid, "route": route}
        plan = {
            "kind": "ticket",
            "title": "QA-1, QA-2, QA-3, QA-4",
            "retest_of": "2026-11-20-x",
            "tickets": [{"key": k, "acs": [{"id": "AC-1"}]} for k in ("QA-1", "QA-2", "QA-3", "QA-4")],
            "cases": [case("TC-1", "QA-1", "/a"), case("TC-2", "QA-2", "/b"), case("TC-4", "QA-4", "/d")],
            "prior_passed": [{**case("TC-3", "QA-3", "/c"), "build": "1.0"}],  # QA-3: only a prior-build pass
            "questions": [{"text": "Q1 [BLOCKING] Which role?", "ticket": "QA-1"}],
        }  # QA-1: an open blocking question
        ran = lambda t, s, c: {"target": t, "build": "1.1", "status": s, "cases": [{"id": c, "status": "PASS"}]}
        d = self.rundir(
            "2026-11-24-QA-1+QA-2+QA-3+QA-4",
            plan=plan,  # QA-2: its run ended BLOCKED; QA-4: a plain pass
            runs=[
                ("a", ran("/a", "COMPLETED", "TC-1")),
                ("b", ran("/b", "BLOCKED", "TC-2")),
                ("d", ran("/d", "COMPLETED", "TC-4")),
            ],
        )
        report.write_report(d)
        want = {"QA-1": "⚠️ PASS WITH NOTES", "QA-2": "⛔ BLOCKED", "QA-3": "⚠️ PASS WITH NOTES", "QA-4": "✅ PASS"}
        r = (d / "REPORT.md").read_text()
        self.assertEqual({k: re.search(rf"^- {k}: (.+?) — ", r, re.M).group(1) for k in want}, want)
        self.assertIn(", ".join(f"{k} {v}" for k, v in want.items()), (d.parent / "README.md").read_text())
        con = core.connect()
        try:
            table = inbox.inbox(con, self.proj, [{"key": k, "fields": {"summary": "s"}} for k in want])
        finally:
            con.close()
        self.assertEqual({k: re.search(rf"\| {k} \|.*\| ([^|]+) \|$", table, re.M).group(1) for k in want}, want)

    def test_a_chained_retest_keeps_the_earlier_prior_passes(self):
        """Run 1 -> retest 2 -> retest 3: what retest 2 carried as prior passes must reach retest 3's plan."""
        plan = {
            **PLAN1,
            "retest_of": "r1",
            "cases": [PLAN1["cases"][1]],
            "prior_passed": [{**PLAN1["cases"][0], "build": "1.42"}],
        }
        d = self.rundir(
            "2026-11-24-PROJ-1",
            plan=plan,
            runs=[("form", {**RUN1, "build": "1.43", "cases": [{"id": "TC-2", "status": "FAIL", "actual": "x"}]})],
        )
        st = runstate._run_state(d)
        self.assertEqual([(c["id"], c["build"]) for c in st["passed_cases"]], [("TC-1", "1.42")])
        self.assertEqual([c["id"] for c in st["retest_cases"]], ["TC-2"])

    def test_a_ticket_carried_only_by_prior_passes_reads_honestly(self):
        """Multi-ticket partial retest: PROJ-2 was not re-run at all. No 0/0/0, no 'unknown build in this run'."""
        plan = {
            **PLAN,
            "retest_of": "2026-11-23-PROJ-1+PROJ-2",
            "cases": [c for c in PLAN["cases"] if c["ticket"] == "PROJ-1"],
            "questions": [],
            "prior_passed": [{**c, "build": "1.42"} for c in PLAN["cases"] if c["ticket"] == "PROJ-2"],
        }
        prior = self.rundir("2026-11-23-PROJ-1+PROJ-2")
        pid = plan["prior_passed"][0]["id"]
        (prior / f"evidence/{pid}.png").write_bytes(b"png")
        run = {**RUN1, "build": "1.43", "cases": [{"id": c["id"], "status": "PASS"} for c in plan["cases"]]}
        d, r = self.render_run([("form", run)], plan=plan)
        self.assertIn("\n\nticket · 2026-11-23 · build 1.43 · retest of 2026-11-23-PROJ-1+PROJ-2\n\n", r)
        self.assertIn(
            "## PROJ-2: Incident list\n\n⚠️ PASS WITH NOTES · build 1.43 · 2 of 2 cases passed (2 on an earlier build) · "
            "no bugs\n",
            r,
        )
        checks = r.split("Post the Jira comment for PROJ-2")[1].split("\n## ")[0]
        self.assertNotIn("unknown in this run", checks)
        self.assertNotIn("No screenshot", checks)
        self.assertIn(
            f"   - Add [../2026-11-23-PROJ-1+PROJ-2/evidence/{pid}.png](../2026-11-23-PROJ-1%2BPROJ-2/evidence/{pid}.png) "
            "(/list) to the comment in Jira.\n",
            checks,
        )
        self.assertIn(
            "AC-1 Sorted by date: passed on build 1.42, not re-run on 1.43.\n  Sort\n  Ties\n",
            (d / "jira-comment-PROJ-2.txt").read_text(),
        )

    def test_partial_retest_verdict_is_pass_with_notes_and_honest_draft(self):
        plan = {
            **PLAN1,
            "retest_of": "2026-11-23-PROJ-1",
            "cases": [PLAN1["cases"][1]],  # only TC-2 retested
            "prior_passed": [{**PLAN1["cases"][0], "build": "1.42"}],
        }
        run_pass = {**RUN1, "build": "1.43", "cases": [{"id": "TC-2", "status": "PASS", "actual": "Error shown"}]}
        d, r = self.render_run([("form", run_pass)], plan=plan)
        self.assertIn(
            "\n\n⚠️ PASS WITH NOTES · build 1.43 · 2026-11-23 · 2 of 2 cases passed (1 on an earlier build) · no bugs · "
            "retest of 2026-11-23-PROJ-1\n\n",
            r,
        )
        self.assertIn("1 case verified on prior build 1.42; not re-run here", r)
        self.assertIn(
            "### AC-1 Saves 0-130: PASS on build 1.42\n\n- **TC-1 Save 130: PASS on build 1.42, not re-run here.**", r
        )
        self.assertIn(
            "   - TC-1 passed on build 1.42 and was not re-run here (retest of 2026-11-23-PROJ-1): the comment says so.\n",
            r,
        )
        draft = (d / "jira-comment.txt").read_text()
        self.assertIn("\nAC-1 Saves 0-130: passed on build 1.42, not re-run on 1.43.\n  Save 130\n", draft)
        self.assertIn("\nAC-2 Rejects 131: passed.\n  Error shown\n", draft)
        # If the retested case fails, the verdict is FAIL and so is the comment
        run_fail = {**RUN1, "build": "1.43", "cases": [{"id": "TC-2", "status": "FAIL", "actual": "Still saves 131"}]}
        d2, r2 = self.render_run([("form", run_fail)], plan=plan)
        self.assertIn("\n\n❌ FAIL · ", r2)
        self.assertTrue((d2 / "jira-comment.txt").read_text().startswith("QA result: FAILED on build 1.43"))


class NewestRun(Rendering):
    """A ticket's latest run is its newest one, whether it ran alone or with other tickets."""

    def age(self, d, seconds=5000):
        import time

        t = time.time() - seconds
        for f in (d, d / ".data/plan.json", *d.glob(".data/run-*.json")):
            os.utime(f, (t, t))

    def test_a_newer_multi_ticket_run_beats_an_older_run_of_the_ticket_alone(
        self,
    ):  # else a retest plans from stale results
        alone = {
            **PLAN,
            "tickets": [PLAN["tickets"][1]],
            "cases": [c for c in PLAN["cases"] if c["ticket"] == "PROJ-2"],
        }
        both = lambda tc3: [
            (
                "list",
                {
                    "target": "/list",
                    "status": "COMPLETED",
                    "build": "2",
                    "cases": [{"id": "TC-3", "status": tc3}, {"id": "TC-4", "status": "PASS"}],
                },
            )
        ]
        self.age(self.rundir("2026-11-20-PROJ-2", plan=alone, runs=both("PASS")))
        self.rundir("2026-11-23-PROJ-1+PROJ-2", plan=PLAN, runs=both("FAIL"))
        lr = runstate.plan_find(self.proj, keys=["PROJ-2"])["latest_run"]
        self.assertEqual((lr["name"], lr["subset_of"]), ("2026-11-23-PROJ-1+PROJ-2", ["PROJ-1", "PROJ-2"]))
        self.assertEqual([c["id"] for c in lr["retest_cases"]], ["TC-3"])
        skill = (
            Path(report.__file__).parents[2] / "references/planning.md"
        ).read_text()  # where the skill sends a retest from an older folder
        self.assertIn("keeps only `latest_run.tickets`", skill)  # not the other ticket of that run: it'd render BLOCKED

    def test_re_rendering_an_older_run_does_not_make_it_the_newest(self):  # nor a vim swap file beside its REPORT.md
        run = lambda st: [
            (
                "form",
                {
                    "target": "/form",
                    "status": "COMPLETED",
                    "build": "1",
                    "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": st}],
                },
            )
        ]
        old = self.rundir("2026-11-20-PROJ-1", plan=PLAN1, runs=run("PASS"))
        self.age(old)
        self.rundir("2026-11-25-PROJ-1", plan=PLAN1, runs=run("FAIL"))
        report.write_report(old)
        self.assertEqual(runstate.plan_find(self.proj, keys=["PROJ-1"])["latest_run"]["name"], "2026-11-25-PROJ-1")
        line = next(x for x in cli("trace", str(self.proj))[1].splitlines() if x.startswith("| PROJ-1"))
        self.assertIn("❌ FAIL", line)

    def test_trace_shows_the_newest_verdict_when_an_older_run_names_the_fix_version(self):
        run = lambda st: [
            (
                "form",
                {
                    "target": "/form",
                    "status": "COMPLETED",
                    "build": "1",
                    "cases": [{"id": "TC-1", "status": "PASS"}, {"id": "TC-2", "status": st}],
                },
            )
        ]
        self.age(self.rundir("2026-11-20-PROJ-1", plan=PLAN1, runs=run("PASS")))  # fix_version 3.5
        no_version = {**PLAN1, "tickets": [{k: v for k, v in PLAN1["tickets"][0].items() if k != "fix_version"}]}
        self.rundir("2026-11-25-PROJ-1", plan=no_version, runs=run("FAIL"))
        line = next(
            x for x in cli("trace", str(self.proj), "--fix-version", "3.5")[1].splitlines() if x.startswith("| PROJ-1")
        )
        self.assertIn("❌ FAIL", line)
        self.assertIn("2026-11-25-PROJ-1", line)


class TicketFolder(Rendering):
    """qa-out/<KEY>/: one folder per ticket, its run, reused by every re-run; older dated folders keep working."""

    age = NewestRun.age

    ran = staticmethod(
        lambda *cases, build="1.42", status="COMPLETED": {
            "target": "/form",
            "build": build,
            "status": status,
            "cases": [{"id": c, "status": s} for c, s in cases],
        }
    )

    def history(self, d):
        f = d / ".data/history.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def test_plan_find_on_a_ticket_folder(self):
        d = self.rundir("PROJ-1", plan=PLAN1)
        found = runstate.plan_find(self.proj, keys=["PROJ-1"])
        self.assertEqual(found["reusable_plan"]["run_dir"], str(d.resolve()))
        self.assertEqual(found["latest_run"]["name"], "PROJ-1")  # the folder is the run, even before it ran
        self.assertFalse(found["latest_run"]["can_retest"])
        (d / ".data/run-form.json").write_text(json.dumps(self.ran(("TC-1", "PASS"), status="INCOMPLETE")))
        found = runstate.plan_find(self.proj, keys=["PROJ-1"])
        lr = found["latest_run"]
        self.assertIsNone(found["reusable_plan"])  # a case ran: the plan is this run's, re-run in place
        self.assertEqual(
            (lr["run_dir"], lr["can_resume"], lr["incomplete_routes"]), (str(d.resolve()), True, ["/form"])
        )
        self.assertEqual([c["id"] for c in lr["retest_cases"]], ["TC-2"])
        self.assertNotIn("subset_of", lr)

    def test_an_old_combined_folder_is_found_until_the_ticket_has_its_own(self):
        both = self.rundir(plan=PLAN, runs=[("form", RUN_FORM)])
        lr = runstate.plan_find(self.proj, keys=["PROJ-1"])["latest_run"]
        self.assertEqual((lr["name"], lr["subset_of"]), (both.name, ["PROJ-1", "PROJ-2"]))
        # the first run in the new layout retests from it: retest_of names the older folder
        one = {**PLAN1, "retest_of": both.name, "cases": PLAN1["cases"][1:], "prior_passed": lr["passed_cases"]}
        mine = self.rundir("PROJ-1", plan=one, runs=[("form", self.ran(("TC-2", "PASS")))])
        self.age(mine)  # older than the combined run: still the ticket's folder
        found = runstate.plan_find(self.proj, keys=["PROJ-1"])
        self.assertEqual(found["latest_run"]["name"], "PROJ-1")
        self.assertNotIn("subset_of", found["latest_run"])
        self.assertIsNone(found["reusable_plan"])
        self.assertEqual(inbox._case_in_chain(mine, "TC-1")["steps"], ["Open form", "Type 130 | save"])
        self.assertEqual(runstate.plan_find(self.proj, keys=["PROJ-1", "PROJ-2"])["latest_run"]["name"], both.name)
        self.assertEqual(runstate.plan_find(self.proj, keys=["PROJ-2"])["latest_run"]["name"], both.name)

    def test_the_newest_ticket_folder_is_the_tickets_latest_qa(self):
        self.age(self.rundir(plan=PLAN, runs=[("form", RUN_FORM)]))
        self.rundir("PROJ-1", plan=PLAN1, runs=[("form", self.ran(("TC-1", "PASS"), ("TC-2", "PASS")))])
        line = next(x for x in cli("trace", str(self.proj))[1].splitlines() if x.startswith("| PROJ-1"))
        cells = [x.strip() for x in line.strip("|").split("|")]
        self.assertEqual((cells[1], cells[5]), ("⚠️ PASS WITH NOTES", "PROJ-1"))  # Latest run: the ticket's folder

    def test_index_and_header_date_a_ticket_folder_by_its_newest_run_file(self):
        d = self.rundir("PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        t = time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))  # local noon: the date folder names use
        os.utime(d / ".data/run-form.json", (t, t))
        e = self.rundir("2026-11-22-form", plan={"kind": "explore", "title": "/form", "tickets": [], "cases": []})
        report.write_report(e)
        report.write_report(d)
        self.assertIn(" · 2026-11-20 · ", (d / "REPORT.md").read_text())  # not plan.created (2026-11-23)
        rows = [x for x in (self.proj / "qa-out/README.md").read_text().splitlines() if x.startswith("| 2026")]
        self.assertEqual(rows[0].split(" | ")[:2], ["| 2026-11-22", "[2026-11-22-form](2026-11-22-form/REPORT.md)"])
        self.assertEqual(
            rows[1],
            "| 2026-11-20 | [PROJ-1 — Speed limit form](PROJ-1/REPORT.md) | ticket | PROJ-1 ⚠️ PASS WITH NOTES | 0 |",
        )
        self.assertEqual(report.run_summary(self.rundir("PROJ-2", plan=PLAN1))[0], "2026-11-23")  # no run: created

    def test_history_one_line_per_date_build_and_ticket(self):
        d = self.rundir("PROJ-1", plan=PLAN1_ANSWERED, runs=[("form", self.ran(("TC-1", "PASS"), ("TC-2", "PASS")))])
        day = time.strftime("%Y-%m-%d", time.localtime((d / ".data/run-form.json").stat().st_mtime))
        report.write_report(d)
        line = {"date": day, "build": "1.42", "ticket": "PROJ-1", "verdict": "PASS"}
        self.assertEqual(self.history(d), [{**line, "cases": {"pass": 2, "fail": 0, "other": 0}, "bugs": 0}])
        report.write_report(d)  # rendered again (the stop hook, then the skill): still one line
        self.assertEqual(len(self.history(d)), 1)
        (d / ".data/run-form.json").write_text(json.dumps(self.ran(("TC-1", "PASS"), ("TC-2", "FAIL"))))
        report.write_report(d)  # the same day and build, re-run: the line is updated, not added
        failed = {**line, "verdict": "FAIL", "cases": {"pass": 1, "fail": 1, "other": 0}, "bugs": 0}
        self.assertEqual(self.history(d), [failed])
        (d / ".data/run-form.json").write_text(
            json.dumps(self.ran(("TC-1", "PASS"), ("TC-2", "BLOCKED"), build="1.43"))
        )
        report.write_report(d)  # a new build: a new line
        h = self.history(d)
        self.assertEqual(h[0], failed)
        self.assertEqual(
            h[1],
            {**line, "build": "1.43", "verdict": "BLOCKED", "cases": {"pass": 1, "fail": 0, "other": 1}, "bugs": 0},
        )

    def test_history_only_for_a_ticket_run_and_never_through_a_link(self):
        plan_only = self.rundir("PROJ-1", plan=PLAN1)
        report.write_report(plan_only)
        explore = self.rundir(
            "2026-11-22-form", plan={"kind": "explore", "tickets": [], "cases": []}, runs=[("form", RUN1)]
        )
        report.write_report(explore)
        self.assertEqual((self.history(plan_only), self.history(explore)), ([], []))
        d = self.rundir("PROJ-2", plan=PLAN1, runs=[("form", RUN1)])
        outside = self.t / "outside.jsonl"
        outside.write_text("mine\n")
        (d / ".data/history.jsonl").symlink_to(outside)
        report.write_report(d)
        self.assertEqual(outside.read_text(), "mine\n")
        self.assertTrue((d / ".data/history.jsonl").is_symlink())
        self.assertTrue((d / "REPORT.md").read_text().startswith("# PROJ-1: "))  # the report itself is written
        (d / ".data/history.jsonl").unlink()
        (d / ".data/history.jsonl").mkdir()  # can't be read or replaced
        (d / "REPORT.md").unlink()
        self.assertEqual(cli("report", str(d))[0], 0)  # best effort: a history problem never fails the report
        self.assertTrue((d / "REPORT.md").is_file())


class NextAction(Rendering):
    """`qa-mem next-action <run> <slug>`: after each explorer, continue, recover the sign-in once, or stop."""

    def folder(self, *runs):
        import time

        self.n = getattr(self, "n", 0) + 1  # a folder of its own each time: an earlier run file would make a streak
        d = self.rundir(f"2026-11-25-multi-{self.n}", plan={"kind": "ticket", "tickets": [], "cases": []})
        for i, (slug, run) in enumerate(runs):
            f = d / f".data/run-{slug}.json"
            f.write_text(json.dumps(run))
            os.utime(f, (time.time() - 100 + i, time.time() - 100 + i))
        return d

    def test_a_page_blocker_continues_a_session_one_recovers_a_browser_one_stops(self):
        blocked = lambda scope=None, summary="403 for this account": {
            "status": "BLOCKED",
            "summary": summary,
            **({"block_scope": scope} if scope else {}),
        }
        d = self.folder(("ok", {"status": "COMPLETED"}), ("admin", blocked("page")))
        self.assertEqual(runstate.next_action(d, "admin")["action"], "continue")
        self.assertEqual(runstate.next_action(d, "ok")["action"], "continue")
        for scope, summary, action in (
            (None, "session expired, run /qa-sdet:qa-login", "recover_sso"),
            (None, "browser profile in use: another qa-sdet browser", "abort"),
            ("browser", "x", "abort"),
            (None, "the page returned 500", "continue"),
        ):
            d = self.folder(("p", blocked(scope, summary)))
            self.assertEqual(runstate.next_action(d, "p")["action"], action, summary)
        self.assertEqual(runstate.next_action(d, "missing")["action"], "abort")  # no run file: the explorer died
        code, out, _ = cli("next-action", str(d), "p")
        self.assertEqual((code, json.loads(out)["block_scope"]), (0, "page"))

    def test_two_page_blockers_in_a_row_stop_the_batch(self):  # the QA app may be down
        d = self.folder(
            ("a", {"status": "BLOCKED", "block_scope": "page", "summary": "500"}),
            ("b", {"status": "BLOCKED", "block_scope": "page", "summary": "500"}),
        )
        self.assertEqual(runstate.next_action(d, "a")["action"], "continue")  # the coordinator asks after each route
        nxt = runstate.next_action(d, "b")
        self.assertEqual(nxt["action"], "abort")
        self.assertIn("2 pages in a row", nxt["reason"])

    def test_a_resumed_run_starts_a_new_streak(self):  # its run files from the stopped session are no streak
        blocked = {"status": "BLOCKED", "block_scope": "page", "summary": "403"}
        d = self.folder(("a", blocked))
        self.assertEqual(runstate.next_action(d, "a")["action"], "continue")
        (d / ".data/run-b.json").write_text(json.dumps({**blocked, "summary": "500"}))
        self.assertEqual(runstate.next_action(d, "b")["action"], "abort")  # 2 in a row: the run stops
        (d / ".data/run-a.json").write_text(json.dumps(blocked))  # --resume: /a first again, still a 403
        os.utime(d / ".data/run-a.json")
        self.assertEqual(runstate.next_action(d, "a")["action"], "continue")
        self.assertEqual(
            runstate.next_action(d, "a")["action"], "continue"
        )  # asked twice about one result: same answer

    def test_a_session_stopped_after_a_page_blocker_does_not_carry_its_streak(self):  # the user interrupted it
        import time

        blocked = {"status": "BLOCKED", "block_scope": "page", "summary": "403"}
        d = self.folder(("a", blocked))
        self.assertEqual(runstate.next_action(d, "a")["action"], "continue")  # one so far
        time.sleep(0.01)
        (d / ".data/brief-b.md").write_text("b")  # /b was dispatched, then the session stopped: no answer for it
        (d / ".data/brief-c.md").write_text("c")  # --resume starts with /c, also blocked
        (d / ".data/run-c.json").write_text(json.dumps(blocked))
        self.assertEqual(runstate.next_action(d, "c")["action"], "continue")
        d2 = self.folder(("a", blocked))
        self.assertEqual(runstate.next_action(d2, "a")["action"], "continue")  # stopped right after this answer
        (d2 / ".data/run-a.json").write_text(json.dumps(blocked))  # --resume runs /a again, still a 403
        os.utime(d2 / ".data/run-a.json", (time.time() + 5, time.time() + 5))
        self.assertEqual(runstate.next_action(d2, "a")["action"], "continue")

    def test_a_sign_in_recovered_mid_row_keeps_the_row(
        self,
    ):  # /b blocked, /c's sign-in recovered, /c blocked: 2 in a row
        import time

        d = self.folder(("b", {"status": "BLOCKED", "block_scope": "page", "summary": "500"}))
        self.assertEqual(runstate.next_action(d, "b")["action"], "continue")
        (d / ".data/run-c.json").write_text(json.dumps({"status": "BLOCKED", "block_scope": "session", "summary": "x"}))
        self.assertEqual(runstate.next_action(d, "c")["action"], "recover_sso")
        (d / ".data/run-c.json").write_text(json.dumps({"status": "BLOCKED", "block_scope": "page", "summary": "500"}))
        os.utime(d / ".data/run-c.json", (time.time() + 5, time.time() + 5))
        self.assertEqual(runstate.next_action(d, "c")["action"], "abort")

    def test_block_scope_is_checked_at_ingest(self):
        from qa_sdet import ingest

        with self.assertRaises(ValueError):
            ingest.check_run({"run_id": "r", "status": "BLOCKED", "block_scope": "planet"})
        ingest.check_run({"run_id": "r", "status": "BLOCKED", "block_scope": "session"})


if __name__ == "__main__":
    unittest.main()
