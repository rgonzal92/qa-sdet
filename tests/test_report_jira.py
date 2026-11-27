"""The Jira comment and the filing blocks of REPORT.md (qa_sdet.report)."""

import json
import re
import unittest
from unittest import mock

from support import PLAN, PLAN1, PLAN1_ANSWERED, RUN1, RUN_FORM, TEMPLATES, Rendering, cli

from qa_sdet import core, inbox, ingest, report, report_jira, results, runstate


class JiraDraft(Rendering):
    """jira-comment.txt: the ticket's Jira comment, for every verdict, and what to check before posting it."""

    def test_a_fail_gets_a_failed_comment_and_stale_drafts_are_removed(self):
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
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=runs)
        for old in ("jira-comment-PROJ-1.md", "jira-comment-PROJ-1.txt", "jira-comment-PROJ-9.md", "jira-comment.md"):
            (d / old).write_text("old draft")  # the .md twin and the old per-key name, or a ticket gone from the plan
        report.write_report(d)
        draft = (d / "jira-comment.txt").read_text()  # a FAILED comment now, never the old or a pass one
        self.assertTrue(draft.startswith("QA result: FAILED on build 1.42 (2026-11-23)\n"), draft)
        self.assertIn("AC-2 Rejects 131: FAILED.\n  131 saved\n", draft)
        self.assertEqual(sorted(p.name for p in d.glob("jira-comment*")), ["jira-comment.txt"])  # stale: removed
        self.assertIn("\n\n❌ FAIL · ", (d / "REPORT.md").read_text())

    def test_a_failed_render_leaves_no_earlier_pass_draft(self):  # "Passed on …" stayed postable after a FAIL
        d, _ = self.render_run([("form", RUN1)])
        self.assertIn("QA result: PASSED WITH NOTES", (d / "jira-comment.txt").read_text())
        failed = {**RUN1, "cases": [RUN1["cases"][0], {"id": "TC-2", "status": "FAIL", "actual": "131 saved"}]}
        (d / ".data/run-form.json").write_text(json.dumps(failed))
        with mock.patch.object(report, "render_report_parts", side_effect=TypeError("boom")):
            report.write_report(d)
        self.assertEqual(sorted(p.name for p in d.glob("jira-comment-*")), [])  # no verdict vouches for them now
        r = (d / "REPORT.md").read_text()
        self.assertIn("Could not render this report: TypeError: boom", r)
        self.assertIn("Jira comment drafts", r)  # says they are gone and why

    def test_an_unreadable_run_file_blocks_the_ticket(self):  # its FAIL and its bug were dropped: a pass draft
        cut = json.dumps({**RUN1, "cases": [{"id": "TC-2", "status": "FAIL"}]})[:-20]  # cut off mid-write
        for runs, why in (([("form", RUN1)], "all cases passed elsewhere"), ([], "the only run file")):
            with self.subTest(why):
                d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1_ANSWERED, runs=runs)
                (d / ".data/run-form-resume.json").write_text(cut)
                report.write_report(d)
                r = (d / "REPORT.md").read_text()
                self.assertIn("\n\n⛔ BLOCKED · ", r)
                self.assertIn("\n\nrun-form-resume.json unreadable: its results are not counted\n\n", r)
                self.assertIn("QA result: BLOCKED", (d / "jira-comment.txt").read_text())  # never a pass
                self.assertIn("PROJ-1 ⛔ BLOCKED", (d.parent / "README.md").read_text())  # the run index agrees
                # a partial --retest must not carry a pass this verdict didn't trust
                self.assertEqual(runstate._run_state(d)["passed_cases"], [])
                for f in (d / ".data").glob("run-*.json"):
                    f.unlink()
        failed = {**RUN1, "cases": [RUN1["cases"][0], {"id": "TC-2", "status": "FAIL", "actual": "131 saved"}]}
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1_ANSWERED, runs=[("form", failed)])
        (d / ".data/run-form-resume.json").write_text(cut)
        report.write_report(d)
        self.assertIn("\n\n❌ FAIL · ", (d / "REPORT.md").read_text())  # a failure it did read still wins

    def test_a_blocked_run_gets_no_pass_draft(self):  # its cases said PASS; the run said BLOCKED
        d, r = self.render_run([("form", {**RUN1, "status": "BLOCKED", "summary": "session expired"})])
        self.assertIn("\n\n⛔ BLOCKED · build 1.42 · ", r)
        self.assertIn("\n\nthe run on /form ended BLOCKED: session expired\n\n", r)
        self.assertTrue((d / "jira-comment.txt").read_text().startswith("QA result: BLOCKED on build 1.42"))
        self.assertIn("⛔ BLOCKED", (d.parent / "README.md").read_text())  # the run index agrees

    def test_build_check_uses_the_builds_of_the_tickets_own_runs(self):  # another ticket's build hid it
        plan = {
            **PLAN1,
            "tickets": PLAN1["tickets"]
            + [{"key": "PROJ-2", "summary": "List", "fix_version": "3.4", "fixed_note": "The list loads."}],
            "cases": PLAN1["cases"] + [{"id": "TC-3", "ticket": "PROJ-2", "title": "List loads", "route": "/list"}],
        }
        for unk in ("unknown", "Unknown", "unknown (missing footer)", "N/A", "undefined", ""):
            runs = [
                ("form", {**RUN1, "build": unk}),
                ("list", {**RUN1, "target": "/list", "build": "3.4.0", "cases": [{"id": "TC-3", "status": "PASS"}]}),
            ]
            d, r = self.render_run(runs, plan=plan)
            one, two = r.split("Post the Jira comment for PROJ-1")[1].split("Post the Jira comment for PROJ-2")
            self.assertIn("- The build is unknown in this run: confirm the QA app runs 3.5.0,", one, unk)
            self.assertNotIn("build is unknown", two.split("\n## ")[0], unk)
            self.assertIn(
                "QA result: PASSED WITH NOTES on an unknown build", (d / "jira-comment-PROJ-1.txt").read_text(), unk
            )
            self.assertIn(" on build 3.4.0 (", (d / "jira-comment-PROJ-2.txt").read_text(), unk)

    def test_draft_takes_one_passing_image_per_route(self):  # one route and two passes proved neither
        plan = {
            **PLAN1,
            "cases": PLAN1["cases"]
            + [{"id": "TC-3", "ticket": "PROJ-1", "ac": "AC-1", "title": "List", "route": "/list"}],
        }
        runs = [
            (
                "form",
                {
                    **RUN1,
                    "cases": [
                        {"id": "TC-1", "status": "QUESTION", "notes": "Order?"},
                        {"id": "TC-2", "status": "PASS"},
                    ],
                },
            ),
            ("list", {**RUN1, "target": "/list", "cases": [{"id": "TC-3", "status": "PASS"}]}),
        ]
        d = self.rundir("2026-11-23-PROJ-1", plan=plan, runs=runs)
        for cid in ("TC-1", "TC-2", "TC-3"):
            (d / "evidence" / f"{cid}.png").write_bytes(b"png")
        self.config(releaseTemplates=TEMPLATES)
        report.write_report(d)
        checks = (d / "REPORT.md").read_text().split("Post the Jira comment")[1].split("\n## ")[0]
        self.assertIn("   - Add TC-2's image (/form) to the comment in Jira.\n", checks)  # TC-1 is first, but no pass
        self.assertIn("   - Add TC-3's image (/list) to the comment in Jira.\n", checks)
        self.assertNotIn("TC-1", checks)

    def test_draft_lines_stay_single_and_true_is_no_text(self):
        plan = {
            **PLAN1,
            "tickets": [{**PLAN1["tickets"][0], "fix_version": True}],
            "cases": [{**c, "route": "/form\ninjected"} for c in PLAN1["cases"]],
        }
        d = self.rundir(
            "2026-11-23-PROJ-1",
            plan=plan,
            runs=[("form", {**RUN1, "not_tested": [True, "Real gap"], "questions": [True]})],
        )
        (d / "evidence/TC-1.png").write_bytes(b"png")
        report.write_report(d)
        r, draft = (d / "REPORT.md").read_text(), (d / "jira-comment.txt").read_text()
        self.assertNotIn("Release:", draft)  # JSON true is no fix version: no release line, and no placeholder
        self.assertIn("   - Add TC-1's image (/form injected) to the comment in Jira.\n", r)
        self.assertNotIn("- True", r)
        self.assertIn("Real gap", r)
        self.config(releaseTemplates=["Rel-\n{version}"])
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        report.write_report(d)
        self.assertIn("\nRelease: Rel- 3.5.0\n", (d / "jira-comment.txt").read_text())

    def test_draft_shows_one_image_per_route_and_no_markup(self):
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1, runs=[("form", RUN1)])
        (d / "evidence").mkdir(exist_ok=True)
        (d / "evidence" / "TC-1.png").write_bytes(b"png")
        (d / "evidence" / "TC-2.png").write_bytes(b"png")
        self.config(releaseTemplates=TEMPLATES)
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertEqual(r.count("'s image ("), 1)  # both cases are on /form: the first passing one only
        self.assertIn("   - Add TC-1's image (/form) to the comment in Jira.\n", r)
        self.assertNotRegex((d / "jira-comment.txt").read_text(), r"[|<>*#_`]|!\[|\]\(")  # plain text for Jira

    def test_missing_templates_or_fix_version_leave_no_line_and_no_placeholder(self):
        d = self.rundir(
            "2026-11-23-PROJ-1",
            plan={**PLAN1, "tickets": [{**PLAN1["tickets"][0], "fix_version": None, "fixed_note": ""}]},
            runs=[("form", RUN1)],
        )
        self.config()  # no releaseTemplates at all
        report.write_report(d)
        draft = (d / "jira-comment.txt").read_text()
        self.assertTrue(draft.startswith("QA result: PASSED WITH NOTES on build 1.42 (2026-11-23)\n\nAC-1 "), draft)
        for absent in ("Release:", "Fixed:", "<"):
            self.assertNotIn(absent, draft)

    def test_pass_with_notes_comment_says_what_is_open(self):
        runs = [
            (
                "form",
                {
                    **RUN1,
                    "cases": [
                        {"id": "TC-1", "status": "PASS"},
                        {"id": "TC-2", "status": "QUESTION", "notes": "Order unclear"},
                    ],
                },
            )
        ]
        d, r = self.render_run(runs)
        self.assertIn("\n\n⚠️ PASS WITH NOTES · ", r)
        draft = (d / "jira-comment.txt").read_text()
        self.assertIn("\nAC-2 Rejects 131: open question.\n  Order unclear\n", draft)
        self.assertTrue(draft.endswith("\n\nOpen question: Is 0 a valid speed?\n"), draft)  # PLAN1's Q1, still open
        self.assertIn("Fixed: Speed no longer accepts 131.\n", draft)

    def test_release_names_fill_the_version(self):
        self.assertEqual(report_jira._release_names(["A-{version}"], "3.5"), (["A-3.5.0"], ""))
        self.assertEqual(report_jira._release_names(["A-{version}"], "3.5.1"), (["A-3.5.1"], ""))
        self.assertEqual(
            report_jira._release_names(["A-{version}"], None)[1],
            "the ticket has no single fix version (plan.json fix_version)",
        )
        self.assertEqual(report_jira._release_names("x", "3.5")[1], "set releaseTemplates in the qa-sdet config")

    def test_round2_report_resilience_types_and_prefixes(self):
        # M2: broken symlink and deeply nested run JSON do not crash report or index
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1_ANSWERED, runs=[("form", RUN1)])
        (d / ".data/run-broken.json").symlink_to(self.t / "missing-target.json")
        (d / ".data/run-deep.json").write_text("[" * 3000 + "]" * 3000)
        r = report.render_report(d)
        self.assertIn("\n\n⛔ BLOCKED · ", r)  # rendered; what they held is unknown
        self.assertIn("run-broken.json unreadable", r)
        self.assertIn("run-deep.json unreadable", r)
        # M5: bool/list case IDs warned & dropped, numeric 0 actual kept, non-string ticket keys ignored in run_summary
        plan_odd = {
            **PLAN1,
            "tickets": [{"key": ["bad"]}, {"key": 12}, *PLAN1["tickets"]],
            "cases": [{"id": True, "title": "bool id"}, {"id": ["x"], "title": "list id"}, *PLAN1["cases"]],
        }
        run_zero = {
            **RUN1,
            "cases": [{"id": "TC-1", "status": "PASS", "actual": 0}, {"id": "TC-2", "status": "PASS", "actual": "ok"}],
            "pages": [{"route": "/form", "coverage": [{"heuristic": "   ", "result": "pass"}]}],
        }
        d2 = self.rundir("2026-11-23-ODD", plan=plan_odd, runs=[("form", run_zero)])
        r2 = report.render_report(d2)
        self.assertIn("- **TC-1 Save 130: PASS.** 0\n", r2)
        self.assertNotIn("True", r2)
        self.assertNotIn("['x']", r2)
        self.assertNotIn("other:", r2)
        summary = report.run_summary(d2)
        self.assertNotIn("['bad']", summary[2])
        # L4: non-ASCII jiraKeyPrefix 'ß' does not strip 'SS-' in inbox
        con = core.connect()
        try:
            out = inbox.inbox(
                con, self.proj, [{"key": "SS-12", "fields": {"summary": "x", "status": {"name": "Open"}}}], prefix="ß"
            )
            self.assertIn("Next: /qa-sdet:qa-ticket SS-12", out)
        finally:
            con.close()


class JiraBundles(Rendering):
    """A Jira comment for every verdict and the command to post it; filed bugs get a comment, not a new bug."""

    def test_a_fail_gets_a_failed_comment_and_a_printed_command(self):
        run = {
            **RUN1,
            "cases": [
                {"id": "TC-1", "status": "PASS"},
                {"id": "TC-2", "status": "FAIL", "actual": "131 saved", "bug": "BUG-1"},
            ],
            "bugs": [
                {
                    "route": "/form",
                    "title": "Speed accepts 131",
                    "symptom": "131 saved",
                    "severity": "HIGH",
                    "classification": "CONFIRMED",
                    "draft_path": "bugs/BUG-1.md",
                }
            ],
        }
        d, r = self.render_run([("form", run)], plan=PLAN1_ANSWERED)
        txt = (d / "jira-comment.txt").read_text()
        self.assertTrue(txt.startswith("QA result: FAILED on build 1.42 (2026-11-23)\n"), txt)
        self.assertIn("\nAC-2 Rejects 131: FAILED.\n  131 saved\n", txt)
        self.assertTrue(txt.endswith("\n\nBug: Speed accepts 131 (HIGH), not filed yet\n"), txt)
        self.assertNotIn("BUG-1", txt)
        self.assertIn(
            f"   ```sh\n   acli jira workitem comment create --key PROJ-1 --body-file {d / 'jira-comment.txt'}\n   ```\n",
            r,
        )
        d2, _ = self.render_run([("form", {**RUN1})], plan=PLAN1_ANSWERED)
        self.assertTrue((d2 / "jira-comment.txt").read_text().startswith("QA result: PASSED on build 1.42"))

    def test_a_bug_already_filed_gets_a_comment_and_a_new_one_a_link_when_configured(self):
        from qa_sdet import ingest

        con = core.connect()
        ingest.ingest(
            con,
            {
                **RUN1,
                "run_id": "old",
                "bugs": [{"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved"}],
            },
        )
        h = core.bug_hash("/form", "131 saved")
        con.execute("UPDATE bugs SET status='REGRESSED', jira_key='PROJ-55' WHERE hash=?", (h,))
        con.commit()
        con.close()
        bugs = [
            {
                "route": "/form",
                "title": "Speed accepts 131",
                "symptom": "131 saved",
                "hash": h,
                "severity": "HIGH",
                "classification": "CONFIRMED",
                "draft_path": "bugs/BUG-1.md",
            },
            {
                "route": "/form",
                "title": "Name accepts 300 chars",
                "symptom": "300 chars",
                "severity": "LOW",
                "classification": "CONFIRMED",
                "draft_path": "bugs/BUG-2.md",
                "ticket": "PROJ-1",
            },
        ]
        self.config(releaseTemplates=TEMPLATES, jiraBugLinkType="Blocks")
        d = self.rundir("2026-11-23-PROJ-1", plan=PLAN1_ANSWERED, runs=[("form", {**RUN1, "bugs": bugs})])
        for i in (1, 2):
            (d / f"bugs/BUG-{i}.md").write_text(f"# t{i}\n")
        (d / ".data/jira-dups.json").write_text(
            json.dumps({"BUG-2": [{"key": "PROJ-77", "summary": "Name too long saved"}]})
        )
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn(
            "1. Comment on PROJ-55 (already in Jira, REGRESSED): [BUG-1](bugs/BUG-1.md) Speed accepts 131 (HIGH)\n", r
        )
        self.assertIn(
            "\nBug: Speed accepts 131 (HIGH), PROJ-55\nBug: Name accepts 300 chars (LOW), not filed yet\n",
            (d / "jira-comment.txt").read_text(),
        )
        one, two = r.split("\n## Bugs\n")[1].split("### BUG-2 ")
        self.assertIn("- already in Jira as PROJ-55 (REGRESSED): add this draft there as a comment:", one)
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
        self.assertEqual(
            json.loads((d / ".data/jira-dups.json").read_text()),
            {"BUG-2": [{"key": "PROJ-77", "summary": "Name too long saved"}]},
        )


class FilingBundle(Rendering):
    """Persistent shell-safe filing commands & attachments folded inside REPORT.md."""

    def test_bug_owners_are_found_once_per_ticket_not_once_per_bug(self):  # bugs x tickets x cases: 5 s at 800 cases
        def lookups(n):
            bugs = [
                {
                    "route": "/form",
                    "title": f"Bug {i}",
                    "severity": "HIGH",
                    "classification": "CONFIRMED",
                    "draft_path": f"bugs/BUG-{i}.md",
                }
                for i in range(1, n + 1)
            ]
            d = self.rundir(f"2026-11-23-PROJ-1+PROJ-2-{n}", plan=PLAN, runs=[("form", {**RUN_FORM, "bugs": bugs})])
            for i in range(1, n + 1):
                (d / f"bugs/BUG-{i}.md").write_text(f"# Bug {i}\n")
            with mock.patch.object(results, "ticket_bugs", wraps=results.ticket_bugs) as spy:
                r = report.render_report(d)
            mine = r.split("## PROJ-1")[1].split("## PROJ-2")[0]
            self.assertEqual(mine.count("- Page /form"), n)  # every untagged bug on /form still counts for PROJ-1
            return spy.call_count

        self.assertEqual(lookups(1), lookups(12))

    def test_confirmed_draft_gets_safe_filing_block_and_suspected_does_not(self):
        import subprocess

        self.config(jiraProject="DEMO")
        title = "Can't save `id` and $(echo INJECTED) at 131"
        bugs = [
            {
                "route": "/form",
                "title": title,
                "symptom": "131 saved",
                "severity": "HIGH",
                "classification": "CONFIRMED",
                "draft_path": "bugs/BUG-1.md",
                "ticket": "PROJ-1",
            },
            {
                "route": "/form",
                "title": "Flaky toast",
                "symptom": "toast",
                "severity": "LOW",
                "classification": "SUSPECTED",
                "draft_path": "bugs/BUG-2.md",
            },
        ]
        d = self.rundir("2026-11-24-PROJ-1", plan=PLAN1, runs=[("form", {**RUN1, "bugs": bugs})])
        (d / "bugs/BUG-1.md").write_text(f"# {title}\n\nbody\n")
        (d / "bugs/BUG-2.md").write_text("# Flaky toast\n\nbody\n")
        (d / "evidence/BUG-1.png").write_bytes(b"png")
        (d / "evidence/BUG-1.webm").write_bytes(b"webm")
        (d / "repro/BUG-1.spec.ts").write_text("test('x', () => {});")
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        one, two = r.split("\n## Bugs\n")[1].split("### BUG-2 ")
        self.assertIn("- File it:\n\n  ```sh\n  acli jira workitem create --project PROJ ", one)
        self.assertNotIn("acli", two)  # SUSPECTED: confirm it first
        self.assertIn("Check the bug: [BUG-2](bugs/BUG-2.md) Flaky toast (LOW, SUSPECTED)", r)
        h = core.bug_hash("/form", "131 saved")
        self.assertIn(f"\n  qa-mem bug-status {h} FILED <JIRA-KEY>\n", one)
        attach = next(ln for ln in one.splitlines() if ln.startswith("  # attach"))
        for name in ("evidence/BUG-1.png", "evidence/BUG-1.webm", "repro/BUG-1.spec.ts"):
            self.assertIn(f"{d}/{name}", attach)
        # Verify the printed acli command is shell-safe (does not execute $(echo INJECTED) or backticks)
        cmd = next(ln.strip() for ln in r.splitlines() if ln.strip().startswith("acli jira workitem create "))
        out = subprocess.run(
            ["bash", "-c", cmd.replace("acli ", "printf '%s\\n' ", 1)], capture_output=True, text=True
        ).stdout
        self.assertNotIn("INJECTED\n", out)
        self.assertIn("$(echo INJECTED)", out)
        # Draft file itself stays clean (no filing command injected into --description-file)
        self.assertNotIn("acli jira", (d / "bugs/BUG-1.md").read_text())

    def test_a_blank_symptom_hashes_the_title_like_ingest(self):
        bug = {
            "route": "/form",
            "title": "Speed accepts 999",
            "symptom": "   ",
            "severity": "HIGH",
            "classification": "CONFIRMED",
            "draft_path": "bugs/BUG-1.md",
        }
        d = self.rundir("2026-11-24-PROJ-1", plan=PLAN1, runs=[("form", {**RUN1, "bugs": [bug]})])
        (d / "bugs/BUG-1.md").write_text("# Speed accepts 999\n")
        report.write_report(d)
        self.assertIn(
            f"qa-mem bug-status {core.bug_hash('/form', 'Speed accepts 999')} FILED", (d / "REPORT.md").read_text()
        )

    def test_a_resighted_bugs_follow_up_names_the_hash_memory_stores(self):  # a reworded regression named no bug
        con = core.connect()
        try:
            ingest.ingest(
                con,
                {
                    "run_id": "r-old",
                    "target": "/form",
                    "build": "1.40",
                    "bugs": [{"route": "/form", "title": "Speed accepts 131", "symptom": "131 saved"}],
                },
                "2026-11-01T00:00:00Z",
            )
            h = core.bug_hash("/form", "131 saved")
            con.execute("UPDATE bugs SET status='FIXED' WHERE hash=?", (h,))
            con.commit()
        finally:
            con.close()
        back = {
            "route": "/form",
            "title": "Speed accepts 131 again",
            "symptom": "131 is saved once more",
            "hash": h,
            "severity": "HIGH",
            "classification": "CONFIRMED",
            "draft_path": "bugs/BUG-1.md",
        }
        jira = {**back, "title": "Known Jira bug", "symptom": "x", "hash": "CAFE-12", "draft_path": "bugs/BUG-2.md"}
        d = self.rundir(
            "2026-11-24-PROJ-1", plan=PLAN1, runs=[("form", {**RUN1, "run_id": "r-new", "bugs": [back, jira]})]
        )
        for n in (1, 2):
            (d / f"bugs/BUG-{n}.md").write_text(f"# Draft {n}\n")
        report.write_report(d)
        follow = re.findall(r"qa-mem bug-status (\S+) FILED", (d / "REPORT.md").read_text())
        self.assertEqual(follow, [h, core.bug_hash("/form", "x")])  # a Jira key is no hash: not 'cafe'
        self.assertEqual(cli("ingest", str(d / ".data/run-form.json"))[:2], (0, "ingested\n"))  # what the hook stores
        code, out, _ = cli("bug-status", follow[0], "FILED", "PROJ-9")
        self.assertEqual((code, out.strip()), (0, f"updated {h} -> FILED (PROJ-9)"))


if __name__ == "__main__":
    unittest.main()
