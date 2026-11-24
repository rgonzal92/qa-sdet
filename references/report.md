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
    "plan": {"scope": "…", "approach": "…", "risks": ["…"], "out_of_scope": ["…"], "test_data": "…"}
  }],
  "cases": [{
    "id": "TC-1", "ticket": "STUDIO-1234", "ac": "AC-1", "title": "…", "route": "/form",
    "priority": "P1 | P2 | P3", "technique": "boundary", "steps": ["…", "…"], "expected": "…"
  }],
  "questions": ["Q1 [BLOCKING] …"]
}
```
- `created`: `date -u +%Y-%m-%dT%H:%M:%SZ`.
- Case ids run TC-1..n across all tickets of the run and are unique (the report shows only the first case with an id); AC ids are per ticket (`ticket` + `ac`). `ac` is one id (`"AC-1"`) or, for a case that checks several, `"AC-1, AC-2"`; each AC's status line in the report counts the cases that name it.
- `priority`: `P1`, `P2` or `P3`. `steps`: a list of strings, one action per item, written so a person can repeat them by hand. `expected`: one string, what a pass looks like, traced to the AC.
- `route`: the page where the case starts; cases are grouped by it, one explorer per route.
- What goes in each plan field: plan-template.md.

## REPORT.md (rendered: keep it as qa-mem writes it)
Summary table (ticket · verdict · why · cases pass/fail/other · bugs), then per ticket: verdict, AC with their status, test plan, and the test case table (steps, expected, status, actual), then bugs (with image links), questions, not tested, and exploration notes per page. Case status icons: ✅ PASS, ❌ FAIL, ⛔ BLOCKED, ⏸️ not run, ❓ QUESTION.

## Verdict per ticket (decided by the renderer)
1. ❌ FAIL: any case FAIL, or a CONFIRMED bug with severity CRITICAL or HIGH.
2. ⛔ BLOCKED: otherwise, any case BLOCKED or not run, or no cases at all.
3. ⚠️ PASS WITH NOTES: otherwise, any QUESTION case, any other bug (MEDIUM/LOW, or SUSPECTED), or an AC with no test case.
4. ✅ PASS: everything else.

Before any run JSON exists (plan written, testing not begun) every ticket shows ⏳ NOT STARTED.

A ticket's bugs: those with its `ticket`, those named by its cases, and (without a ticket field) those found on its cases' routes. An untagged bug that a case of another ticket names belongs to that ticket only, not to every ticket sharing the route.
