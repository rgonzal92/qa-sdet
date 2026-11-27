"""`qa-mem audit-bugs` and the explorer's stop check (qa_sdet.audit)."""

import json
import unittest

from support import GOOD_SPEC, PLAN1_ANSWERED, RUN1, Base, Rendering, cli, good_draft

from qa_sdet import audit, core, report


class ExploreStopCheck(Base):
    """verify_run's explore-mode check: a COMPLETED page that left the card's open items unmentioned is sent back once."""

    CARD = {
        "states": "UNVISITED STATES (on this page, not opened on build 1): tab: Layers",
        "apis": "UNEXERCISED ENDPOINTS (mapped in the code, not yet observed on build 1.0): GET /api/closures",
        "rules": "UNTESTED TARGETS (code rules no adversarial check covered on build 1): Speed",
    }

    def stop(self, card=(), mode="explore", **run_fields):
        d = self.proj / "qa-out/2026-11-25-explore"
        (d / ".data").mkdir(parents=True, exist_ok=True)
        (d / ".data/brief-form.md").write_text(
            "# Explorer brief: /form\nroute: /form\nmode: "
            + mode
            + "\n\n## Card\nPAGE /form\n"
            + "\n".join(self.CARD[k] for k in card)
            + "\nUNTRIED: boundary\n"
        )
        for k in ("requests", "console"):
            (d / f".data/{k}-form.log").write_text("")
        (d / ".data/run-form.json").write_text(
            json.dumps({"run_id": "r", "mode": mode, "status": "COMPLETED", **run_fields})
        )
        return audit.verify_run(d, "form")

    def test_a_completed_explore_page_that_mentions_none_of_the_cards_open_items_is_sent_back_once(self):
        todo = self.stop(["states", "apis"])
        self.assertEqual(len(todo), 1)  # one instruction line: the explorer gets one fix turn
        self.assertIn("UNVISITED STATES", todo[0])
        self.assertIn("UNEXERCISED ENDPOINTS", todo[0])
        self.assertNotIn("UNTESTED TARGETS", todo[0])
        self.assertIn("not_tested", todo[0])

    def test_any_coverage_or_not_tested_entry_counts_as_having_looked(self):
        cov = [{"route": "/form", "coverage": [{"target": "Speed", "heuristic": "boundary", "result": "pass"}]}]
        self.assertEqual(self.stop(["rules"], pages=cov), [])
        self.assertEqual(self.stop(["rules"], not_tested=[{"category": "heuristic", "text": "read-only"}]), [])
        self.assertEqual(self.stop(["rules"], not_tested=["no time"]), [])

    def test_only_a_completed_explore_run_with_open_items_is_checked(self):
        self.assertEqual(self.stop(["states"], mode="ticket"), [])
        self.assertEqual(self.stop([]), [])
        self.assertEqual(self.stop(["states"], status="INCOMPLETE"), [])
        self.assertEqual(self.stop(["states"], status="BLOCKED"), [])


class AuditBugs(Rendering):
    """`qa-mem audit-bugs <run>`: bug drafts and repro specs checked against references/bug-draft.md and the run JSON."""

    H = core.bug_hash("/form", "131 saved")

    def folder(self, draft, spec, png=True, name="2026-11-25-PROJ-1", **bug):
        b = {
            "route": "/form",
            "title": "Speed accepts 131",
            "symptom": "131 saved",
            "severity": "HIGH",
            "classification": "CONFIRMED",
            "draft_path": "bugs/BUG-1.md",
            **bug,
        }
        d = self.rundir(name, plan=PLAN1_ANSWERED, runs=[("form", {**RUN1, "bugs": [b]})])
        for path, body in (("bugs/BUG-1.md", draft), ("repro/BUG-1.spec.ts", spec)):
            if body is not None:
                (d / path).write_text(body)
        if png:
            (d / "evidence/BUG-1.png").write_bytes(b"png")
        return d

    def issues(self, d):
        return [(crit, text) for crit, text in audit.audit_bugs(d).get("BUG-1", [])]

    def test_a_complete_draft_and_spec_pass_and_the_report_says_so(self):
        d = self.folder(good_draft(self.H), GOOD_SPEC)
        self.assertEqual(audit.audit_bugs(d), {})
        report.write_report(d)
        self.assertIn("repro ✓", (d / "REPORT.md").read_text())

    def test_what_makes_a_repro_useless_is_critical(self):
        for n, (draft, spec, want) in enumerate(
            (
                (good_draft(self.H), GOOD_SPEC.replace("await expect(", "await ("), "no expect("),
                (good_draft("0123456789ab"), GOOD_SPEC, "hash"),
                (good_draft(self.H), None, "no repro spec"),
                (None, GOOD_SPEC, "no draft"),
            )
        ):
            d = self.folder(draft, spec, name=f"2026-11-25-case-{n}")
            crit = [t for c, t in self.issues(d) if c]
            self.assertTrue(any(want in t for t in crit), (want, self.issues(d)))
            report.write_report(d)
            self.assertIn("· ⚠ ", (d / "REPORT.md").read_text().split("## Bugs")[1].split("\n## ")[0])
        code, out, _ = cli("audit-bugs", str(d))
        self.assertEqual(code, 0)
        self.assertIn("BUG-1", out)

    def test_a_review_shows_next_to_its_bug_and_never_changes_the_verdict(self):
        d = self.folder(good_draft(self.H), GOOD_SPEC)
        review = {
            "bugs": [
                {"id": "BUG-1", "verdict": "needs_check", "oracle_ok": False, "notes": "the oracle cites the code"}
            ]
        }
        (d / ".data/review.json").write_text(json.dumps(review))
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn("⚠ check before filing: the oracle cites the code", r)
        self.assertIn("\n\n❌ FAIL · ", r)  # the review is a note for the user, not a verdict
        (d / ".data/review.json").write_text(json.dumps({"bugs": [{"id": "BUG-1", "verdict": "solid"}]}))
        self.assertIn("reviewed ✓", report.render_report(d))
        (d / ".data/review.json").write_text("{oops")
        self.assertIn("review.json unreadable", report.render_report(d))

    def test_network_and_repro_are_linked_and_files_are_scrubbed_before_the_report(self):
        d = self.folder(good_draft(self.H) + "Authorization: Bearer abcdefghijklmnopqrst\n", GOOD_SPEC)
        (d / "evidence/BUG-1-network.txt").write_text('POST /api/zones => 500\n{"code": "ZONE_OVERLAP"}\n')
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        row = next(x for x in r.splitlines() if x.startswith("- [draft](bugs/BUG-1.md)"))
        self.assertIn("[repro](repro/BUG-1.spec.ts)", row)
        self.assertIn("[network](evidence/BUG-1-network.txt)", row)
        self.assertIn("evidence/BUG-1-network.txt", r.split("- File it:")[1])  # attached to the Jira bug too
        self.assertNotIn("abcdefghijklmnopqrst", (d / "bugs/BUG-1.md").read_text())  # scrubbed before it was linked

    def test_a_checked_case_without_its_image_is_an_evidence_gap(self):
        d = self.rundir("2026-11-25-gap", plan=PLAN1_ANSWERED, runs=[("form", RUN1)])
        (d / "evidence/TC-1.png").write_bytes(b"png")
        (d / "evidence/TC-2.png").write_bytes(b"")  # 0 bytes: no image
        report.write_report(d)
        r = (d / "REPORT.md").read_text()
        self.assertIn(", after these checks:\n\n   - TC-2 has no evidence image: take it or say why.\n", r)
        self.assertNotIn("TC-1 has no evidence image", r)

    def test_contract_warnings(self):
        spec = (
            GOOD_SPEC.replace("'/form'", "'/incidents/:id'").replace(
                "...(process.env.STORAGE_STATE ? { storageState: process.env.STORAGE_STATE } : {})", ""
            )
            + "await page.waitForTimeout(500); await page.goto('https://qa.example/x');\n"
        )
        draft = good_draft(self.H, repro="1/2", step1="1. Open /form", oracle="", title="#")
        warn = [t for c, t in self.issues(self.folder(draft, spec, png=False)) if not c]
        for want in (
            ":id",
            "waitForTimeout",
            "absolute URL",
            "STORAGE_STATE",
            "Step 1",
            "Oracle",
            "Repro 1/",
            "title",
            "evidence image",
        ):
            self.assertTrue(any(want in t for t in warn), (want, warn))
        self.assertEqual(audit.spec_issues(GOOD_SPEC), [])
        self.assertEqual(audit.draft_issues(good_draft(self.H)), [])


if __name__ == "__main__":
    unittest.main()
