# plan.json and the report

The skill writes `<run>/.data/plan.json`; explorers write `<run>/.data/run-<slug>.json` (run-json.md). `qa-mem report '<run>'` renders both into `<run>/REPORT.md` and updates the index `<project>/qa-out/README.md`. Render after writing plan.json and after each explorer; it renders whatever exists so far.

## plan.json
```json
{
  "kind": "ticket | explore | plan | map",
  "title": "STUDIO-1234, STUDIO-1250",
  "created": "2026-11-23T18:00:00Z",
  "env": {"url": "<qaBaseUrl or target URL>", "browser": "<config browser, or mcp>"},
  "tickets": [{
    "key": "STUDIO-1234",
    "summary": "ticket title",
    "acs": [{"id": "AC-1", "text": "as the ticket states it"}],
    "plan": {"scope": "…", "approach": "…", "risks": ["…"], "out_of_scope": ["…"], "test_data": "…"},
    "fix_version": "3.5",
    "fixed_note": "one sentence: what was fixed (optional, ≤300 chars; used by the Jira draft)"
  }],
  "cases": [{
    "id": "TC-1", "ticket": "STUDIO-1234", "ac": "AC-1", "title": "…", "route": "/form",
    "priority": "P1 | P2 | P3", "technique": "boundary", "steps": ["…", "…"], "expected": "…"
  }],
  "questions": ["Q1 [BLOCKING] …", {"text": "…", "status": "open | resolved", "answer": "…when resolved"}]
}
```
- `created`: `date -u +%Y-%m-%dT%H:%M:%SZ`.
- Case ids run TC-1..n across all tickets of the run and are unique (the report shows only the first case with an id); AC ids are per ticket (`ticket` + `ac`). `ac` is one id (`"AC-1"`) or, for a case that checks several, `"AC-1, AC-2"`; each AC's status line in the report counts the cases that name it.
- `priority`: `P1`, `P2` or `P3`. `steps`: a list of strings, one action per item, written so a person can repeat them by hand. `expected`: one string, what a pass looks like, traced to the AC.
- `route`: the page where the case starts; cases are grouped by it, one explorer per route.
- `fix_version` (optional): the ticket's Jira fix version, as written (`3.5`). It fills `{version}` in the release names. `fixed_note` (optional): what was fixed, in one sentence.
- What goes in each plan field: plan-template.md.

## REPORT.md (rendered: keep it as qa-mem writes it)
Top to bottom:
- Header and the verdict. One ticket: a verdict line with counts (bugs, open questions, cases that need attention, evidence images) and a link to the Jira pass-comment draft when one exists. Two or more tickets: the summary table (ticket · verdict · why · cases pass/fail/other · bugs).
- `## Needs your action`: bugs to file (table with draft and image links), open questions (blocking first; a question on several pages is one line with its pages), failed, blocked and not-run cases. Nothing listed means "Nothing needs your action."
- Per ticket: acceptance criteria with their status, then the compact test case table (ID, priority, AC, page when the cases span pages, case, status, actual, evidence link). Steps, expected, actual and notes are in one folded block per case; non-pass cases open by default. The test plan is a folded block after the cases.
- Not tested: one folded block per category (safety rules, out of scope, not reached, needs setup, heuristics, other), each with its count.
- Resolved questions: one folded block with each answer.
- Exploration notes: one folded block per page, with its heuristic coverage in the title (✅ checked, ❌ problem found, ⛔ could not be checked).
- Problems reading this folder, when there are any.

Plan-only reports (no run yet) show the summary table and the questions, and no action block.

Case images: `evidence/TC-<n>.png` for a case, taken when its result is read. The table links it and the case's folded block embeds it when the file exists.

Jira pass comment: `jira-comment-<KEY>.md`, written next to REPORT.md for each ticket whose verdict is PASS or PASS WITH NOTES. It is plain text: a CHECK block (not for Jira: build, images to paste, placeholders), then "Passed on <release names>", "Fixed: <note>", and one `TC-n - RESULT: title` line per case. Release names come from the config key `releaseTemplates` (`{version}` = the ticket's `fix_version`; 3.5 becomes 3.5.0). The renderer never posts to Jira; a missing name or note stays a visible placeholder. Remove the CHECK block before pasting. Every render rewrites the draft (and removes it once the verdict is no longer a pass), so fill placeholders in Jira or in a copy, not in the file.

## Verdict per ticket (decided by the renderer)
1. ❌ FAIL: any case FAIL, or a CONFIRMED bug with severity CRITICAL or HIGH.
2. ⛔ BLOCKED: otherwise, any case BLOCKED or not run, or no cases at all.
3. ⚠️ PASS WITH NOTES: otherwise, any QUESTION case, any other bug (MEDIUM/LOW, or SUSPECTED), or an AC with no test case.
4. ✅ PASS: everything else.

Before any run JSON exists (plan written, testing not begun) every ticket shows ⏳ NOT STARTED.

A ticket's bugs: those with its `ticket`, those named by its cases, and (without a ticket field) those found on its cases' routes. An untagged bug that a case of another ticket names belongs to that ticket only, not to every ticket sharing the route.
