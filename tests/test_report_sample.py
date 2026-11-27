"""Two whole sample reports and their Jira comments (qa_sdet.report), compared line by line."""

import json
import os
import time
import unittest

from support import GOOD_SPEC, PLAN, RUN1, RUN_FORM, RUN_LIST, TEMPLATES, Rendering, good_draft, spacing_problems

from qa_sdet import core, report

SAMPLE_PROJ_1 = """# PROJ-1: Speed limit form

❌ FAIL · build 1.42 · 2026-11-23 · 1 of 2 cases passed · 1 bug

TC-2 failed (AC-2); BUG-1 HIGH

## What to do

1. File the bug: [BUG-1](bugs/BUG-1.md) Speed accepts 131 (HIGH)

2. Answer: Is 0 a valid speed? _(from /form)_

3. Post the Jira comment: [jira-comment.txt](jira-comment.txt), after these checks:

   - TC-2 has no evidence image: take it or say why.

   ```sh
   acli jira workitem comment create --key PROJ-1 --body-file {run}/jira-comment.txt
   ```

## Results

<details><summary>Test plan</summary>

- **Scope:** speed field
- **Risks:**
  - rounding
- **Out of scope:**
  - mobile

</details>

### AC-1 Saves 0-130: PASS

- **TC-1 Save 130: PASS.** Saved · [image](evidence/TC-1.png)

  <details><summary>Steps</summary>

  Page: /form · priority P1

  1. Open form
  2. Type 130 \\| save

  Expected: Saved

  </details>

### AC-2 Rejects 131: FAIL

- **TC-2 Reject 131: FAIL ([BUG-1](bugs/BUG-1.md)).** 131 saved

  <details open><summary>Steps</summary>

  Page: /form

  1. Type 131

  Expected: Error shown

  </details>

## Bugs

### BUG-1 Speed accepts 131 (HIGH, CONFIRMED)

- Page /form · found by TC-2 (AC-2)

- [draft](bugs/BUG-1.md) · [image](evidence/BUG-1.png) · [repro](repro/BUG-1.spec.ts) · repro ✓

- File it:

  ```sh
  acli jira workitem create --project PROJ --type Bug --summary 'Speed accepts 131' --description-file {run}/bugs/BUG-1.md
  qa-mem bug-status {hash} FILED <JIRA-KEY>
  # attach (look at images and videos first): {run}/evidence/BUG-1.png {run}/repro/BUG-1.spec.ts
  ```

## Not tested

<details><summary>Other (1)</summary>

- Keyboard only: out of time

</details>

## Exploration notes

App: https://qa.example · browser chrome

Heuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked

<details><summary>/form (run-form.json: COMPLETED, build 1.42) — boundary ❌, happy-path ✅</summary>

Explored the speed field.

</details>

## History

| Date | Build | Verdict | Cases | Bugs |
|---|---|---|---|---|
| 2026-11-23 | 1.42 | ❌ FAIL | 1 passed, 1 failed, 0 other | 1 |
"""
SAMPLE_COMMENT_1 = """QA result: FAILED on build 1.42 (2026-11-23)

AC-1 Saves 0-130: passed.
  Saved

AC-2 Rejects 131: FAILED.
  131 saved

Bug: Speed accepts 131 (HIGH), not filed yet
"""
SAMPLE_PROJ_2 = """# PROJ-2: Incident list

⚠️ PASS WITH NOTES · build 1.42 · 2026-11-23 · 1 of 2 cases passed · 1 bug

TC-4 open question; BUG-2 LOW

## What to do

1. File the bug: [BUG-2](bugs/BUG-2.md) Header misaligned (LOW)

2. Answer the question TC-4 raised: see Results.

3. Post the Jira comment: [jira-comment.txt](jira-comment.txt), after these checks:

   - TC-3 has no evidence image: take it or say why.
   - No screenshot for this ticket yet: add one before you post.

   ```sh
   acli jira workitem comment create --key PROJ-2 --body-file {run}/jira-comment.txt
   ```

## Results

### AC-1 Sorted by date: QUESTION

- **TC-3 Sort: PASS.**

  <details><summary>Steps</summary>

  Page: /list

  1. Open

  Expected: Newest first

  </details>

- **TC-4 Ties: QUESTION.** Order of ties unspecified

  <details open><summary>Steps</summary>

  Page: /list

  1. Open

  Expected: Stable

  </details>

## Bugs

### BUG-2 Header misaligned (LOW, CONFIRMED)

- Page /list

- [draft](bugs/BUG-2.md) · ⚠ no repro spec repro/BUG-2.spec.ts (+7: qa-mem audit-bugs)

- File it:

  ```sh
  acli jira workitem create --project PROJ --type Bug --summary 'Header misaligned' --description-file {run}/bugs/BUG-2.md
  qa-mem bug-status {hash} FILED <JIRA-KEY>
  ```

## Exploration notes

App: https://qa.example · browser chrome

Heuristics: ✅ checked, no problem · ❌ problem found · ⛔ could not be checked

<details><summary>/list (run-list.json: COMPLETED, build 1.42)</summary>

s

</details>

## History

| Date | Build | Verdict | Cases | Bugs |
|---|---|---|---|---|
| 2026-11-23 | 1.42 | ⚠️ PASS WITH NOTES | 1 passed, 0 failed, 1 other | 1 |
"""
SAMPLE_COMMENT_2 = """QA result: PASSED WITH NOTES on build 1.42 (2026-11-23)

AC-1 Sorted by date: open question.
  Sort: passed.
  Ties: open question. Order of ties unspecified

Bug: Header misaligned (LOW), not filed yet
"""


class Sample(Rendering):
    """The two-ticket fixture as two ticket folders, REPORT.md and jira-comment.txt compared whole; each fact once."""

    PLANS = {
        "PROJ-1": {**PLAN, "title": "PROJ-1", "tickets": PLAN["tickets"][:1], "cases": PLAN["cases"][:2]},
        "PROJ-2": {**PLAN, "title": "PROJ-2", "tickets": PLAN["tickets"][1:], "cases": PLAN["cases"][2:]},
    }

    def folder(self, key, runs, files=(), plan=None):
        d = self.rundir(key, plan=plan or self.PLANS[key], runs=runs)
        noon = time.mktime((2026, 11, 23, 12, 0, 0, 0, 0, -1))  # local: the day _run_date and the history give
        for f in (d / ".data").glob("run-*.json"):
            os.utime(f, (noon, noon))
        for rel, body in dict(files).items():
            (d / rel).write_bytes(body if isinstance(body, bytes) else body.encode())
        report.write_report(d)
        return d, (d / "REPORT.md").read_text()

    def sample(self):
        h = core.bug_hash("/form", "Speed accepts 131")
        one = self.folder(
            "PROJ-1",
            [("form", RUN_FORM)],
            {
                "bugs/BUG-1.md": good_draft(h),
                "repro/BUG-1.spec.ts": GOOD_SPEC,
                "evidence/BUG-1.png": b"png",
                "evidence/TC-1.png": b"png",
            },
        )
        two = self.folder("PROJ-2", [("list", RUN_LIST)], {"bugs/BUG-2.md": "# Header misaligned\n"})
        return one, two

    def test_the_fixture_renders_as_designed(self):
        (d1, r1), (d2, r2) = self.sample()
        fill = lambda text, d, h: text.replace("{run}", str(d.resolve())).replace("{hash}", h)
        self.assertEqual(r1, fill(SAMPLE_PROJ_1, d1, core.bug_hash("/form", "Speed accepts 131")))
        self.assertEqual(r2, fill(SAMPLE_PROJ_2, d2, core.bug_hash("/list", "Header misaligned")))
        self.assertEqual((d1 / "jira-comment.txt").read_text(), SAMPLE_COMMENT_1)
        self.assertEqual((d2 / "jira-comment.txt").read_text(), SAMPLE_COMMENT_2)
        self.assertEqual(sorted(p.name for p in d1.glob("jira-comment*")), ["jira-comment.txt"])  # no .md twin
        idx = (self.proj / "qa-out/README.md").read_text()
        self.assertIn("| [PROJ-1 — Speed limit form](PROJ-1/REPORT.md) | ticket | PROJ-1 ❌ FAIL | 1 |", idx)

    def test_every_heading_table_fence_and_fold_has_blank_lines_around_it(self):
        (_, r1), (_, r2) = self.sample()
        both = self.rundir(plan=PLAN, runs=[("form", RUN_FORM), ("list", RUN_LIST)])
        report.write_report(both)
        explore = self.rundir("2026-11-24-form", runs=[("form", {**RUN_FORM, "mode": "explore", "cases": []})])
        plan_only = self.rundir("2026-11-24-plan", plan={**PLAN, "kind": "plan", "questions": ["Units?"]})
        for text in (
            r1,
            r2,
            report.render_report(both),
            report.render_report(explore),
            report.render_report(plan_only),
        ):
            self.assertEqual(spacing_problems(text), [], text)
            self.assertNotIn("\n\n\n", text)

    def test_a_long_actual_is_shown_whole_once_in_the_report_and_once_in_the_comment(self):
        long = "The form saved 131 km/h and showed 'Saved'; " * 9 + "after a reload the field still shows 131."
        self.assertGreater(len(long), 400)
        run = {**RUN_FORM, "cases": [RUN_FORM["cases"][0], {**RUN_FORM["cases"][1], "actual": long}]}
        d, r = self.folder("PROJ-1", [("form", run)])
        comment = (d / "jira-comment.txt").read_text()
        self.assertEqual((r.count(long), comment.count(long)), (1, 1))
        self.assertIn(f"  {long}\n", comment)  # on its line, never cut
        self.assertNotIn("…", r + comment)

    def test_the_comment_lists_a_bug_once_with_no_local_ids(self):
        (d1, _), (d2, _) = self.sample()
        for d in (d1, d2):
            comment = (d / "jira-comment.txt").read_text()
            self.assertEqual(comment.count("Bug: "), 1, comment)  # a case's BUG-1 is no Jira key
            self.assertNotRegex(comment, r"BUG-|TC-|/qa-out/|[|*#]")
        jira = {**RUN_FORM, "cases": [RUN_FORM["cases"][0], {**RUN_FORM["cases"][1], "bug": "BUG-1, PROJ-55"}]}
        d, _ = self.folder("PROJ-1", [("form", jira)])
        self.assertIn(
            "Bug: Speed accepts 131 (HIGH), not filed yet\nBug: PROJ-55\n", (d / "jira-comment.txt").read_text()
        )

    def test_the_comment_names_a_known_bug_by_its_hash(self):
        """Found live: a case said "Known bug 61e2ff0fb387" and the Bug line gave a title only: nothing tied the two,
        or the line to REPORT.md's `known bug 61e2ff0fb387`."""
        known = {k: v for k, v in RUN_FORM["bugs"][0].items() if k not in ("draft_path", "evidence", "annotated")}
        run = {
            **RUN_FORM,
            "cases": [RUN_FORM["cases"][0], {**RUN_FORM["cases"][1], "bug": "61e2ff0fb387"}],
            "bugs": [{**known, "hash": "61e2ff0fb387"}],
        }
        d, r = self.folder("PROJ-1", [("form", run)])
        self.assertIn("known bug 61e2ff0fb387", r)
        self.assertTrue(
            (d / "jira-comment.txt")
            .read_text()
            .endswith("\n\nBug: Speed accepts 131 (HIGH), known bug 61e2ff0fb387, not filed yet\n")
        )

    def test_a_question_case_whose_question_is_listed_is_one_thing_to_do(self):
        """Found live: an explorer marked a case QUESTION and wrote its question in `questions`: What to do said twice
        to answer it, once by its text and once by its case."""
        todo = lambda r: r.split("## What to do")[1].split("\n## ")[0]
        for asked in ("TC-4: do ties keep the order they were added in?", "Order of ties unspecified: which is first?"):
            with self.subTest(asked=asked):
                self.n = getattr(self, "n", 0) + 1
                d = self.rundir(
                    f"PROJ-2-{self.n}", plan=self.PLANS["PROJ-2"], runs=[("list", {**RUN_LIST, "questions": [asked]})]
                )
                report.write_report(d)
                steps = todo((d / "REPORT.md").read_text())
                self.assertIn(f"Answer: {asked}", steps)
                self.assertNotIn("Answer the question TC-4 raised", steps)
        d, r = self.folder("PROJ-2", [("list", {**RUN_LIST, "questions": ["Is the header fixed?"]})])
        self.assertIn("Answer the question TC-4 raised: see Results.", todo(r))  # another question: both stand

    def test_no_comment_holds_a_placeholder(self):
        """Release and Fixed lines only when known; an unknown build said in words; never `<…>` to fill in."""
        quiet = json.loads(json.dumps(self.PLANS["PROJ-1"]))
        quiet["tickets"][0].pop("plan")
        for templates, fix, build in ((None, None, "unknown"), (TEMPLATES, None, ""), (TEMPLATES, "3.5", "1.42")):
            with self.subTest(templates=templates, fix=fix, build=build):
                self.config(**({"releaseTemplates": templates} if templates else {}))
                plan = {
                    **quiet,
                    "tickets": [{**quiet["tickets"][0], "fix_version": fix, "fixed_note": fix and "No 131."}],
                }
                for cases in (RUN1["cases"], RUN_FORM["cases"]):
                    run = {**RUN_FORM, "build": build, "cases": cases, "bugs": []}
                    d, r = self.folder("PROJ-1", [("form", run)], plan=plan)
                    comment = (d / "jira-comment.txt").read_text()
                    self.assertNotRegex(comment, r"<[A-Za-z]", comment)
                    self.assertEqual("Release: Release-A-3.5.0, Release-B-3.5.0" in comment, bool(fix), comment)
                    self.assertEqual("Fixed: No 131." in comment, bool(fix) and cases is RUN1["cases"], comment)
                    if build != "1.42":
                        self.assertTrue(comment.startswith("QA result: "), comment)
                        self.assertIn(" on an unknown build (2026-11-23)\n", comment.splitlines()[0] + "\n")
                        self.assertIn("The build is unknown in this run: confirm", r)

    def test_history_rows_newest_first_and_earlier_bugs_this_run_did_not_see(self):
        d = self.rundir("PROJ-1", plan=self.PLANS["PROJ-1"])
        lines = [
            {
                "date": "2026-11-20",
                "build": "1.40",
                "ticket": "PROJ-1",
                "verdict": "FAIL",
                "cases": {"pass": 0, "fail": 2, "other": 0},
                "bugs": 2,
            },
            {
                "date": "2026-11-22",
                "build": "1.41",
                "ticket": "PROJ-1",
                "verdict": "BLOCKED",
                "cases": {"pass": 1, "fail": 0, "other": 1},
                "bugs": 0,
            },
            {"date": "2026-11-22", "build": "1.41", "ticket": "PROJ-9", "verdict": "PASS", "cases": {}, "bugs": 0},
        ]
        (d / ".data/history.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        (d / "bugs/BUG-3.md").write_text("# Old title\n")
        run = {
            **RUN_FORM,
            "build": "1.42",
            "bugs": [],
            "cases": [{**RUN_FORM["cases"][0]}, {"id": "TC-2", "status": "PASS"}],
        }
        d, r = self.folder("PROJ-1", [("form", run)])
        history = r.split("## History\n\n", 1)[1]
        rows = [x for x in history.splitlines() if x.startswith("| 2026")]
        self.assertEqual(
            rows,
            [
                "| 2026-11-23 | 1.42 | ✅ PASS | 2 passed, 0 failed, 0 other | 0 |",
                "| 2026-11-22 | 1.41 | ⛔ BLOCKED | 1 passed, 0 failed, 1 other | 0 |",
                "| 2026-11-20 | 1.40 | ❌ FAIL | 0 passed, 2 failed, 0 other | 2 |",
            ],
        )  # this run's line first, another ticket's left out
        self.assertIn(
            "Earlier bugs this run did not see (no verdict counts them; file one if it still applies):\n\n"
            "- [BUG-3](bugs/BUG-3.md) Old title\n",
            history,
        )
        self.assertNotIn("BUG-3", r.split("## History")[0])  # no verdict counts it: not something to file now
        (d / ".data/history.jsonl").unlink()
        (d / "bugs/BUG-3.md").unlink()
        self.assertNotIn("## History", report.render_report(d))  # no file and no earlier draft: no section


if __name__ == "__main__":
    unittest.main()
