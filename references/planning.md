# plan.json and test design

The skill writes `<run>/.data/plan.json`; `qa-mem report '<run>'` renders it.

```json
{
  "kind": "ticket",
  "title": "PROJ-1234, PROJ-1250",
  "created": "2026-11-23T18:00:00Z",
  "env": {"url": "<qaBaseUrl>", "browser": "<config browser>"},
  "tickets": [{
    "key": "PROJ-1234", "summary": "ticket title",
    "acs": [{"id": "AC-1", "text": "as the ticket states it"}],
    "plan": {"scope": "AC-1 speed limit", "approach": "UI, P1 first", "risks": ["Wrong limit saved: data error (TC-1)"],
             "out_of_scope": ["Provoking a zones API error (needs --faults)"], "test_data": "role editor; qa-sdet-zone-10"},
    "fix_version": "3.5", "fixed_note": "optional, one sentence"
  }],
  "cases": [{"id": "TC-1", "ticket": "PROJ-1234", "ac": "AC-1", "title": "Speed 131 is rejected", "route": "/form",
             "priority": "P1", "technique": "BVA", "steps": ["Type 131 in Speed limit", "Click Save"],
             "expected": "Error 'Max 130' shown; nothing saved"}],
  "questions": ["Q1 [BLOCKING] Is 130 itself allowed?", {"text": "Q2 [HIGH] …", "ticket": "PROJ-1234"},
                {"text": "…", "status": "resolved", "answer": "…"}]
}
```
`kind` is `ticket` (qa-ticket), `plan` (qa-plan) or `explore` (qa-explore). `created` is `date -u +%Y-%m-%dT%H:%M:%SZ`. `fix_version` and `fixed_note` are optional and fill the Jira pass-comment draft. Copy each ticket's `source_snapshot` from `qa-mem prep` onto its `tickets[]` entry (what the plan was made against: a later `--retest` compares it with Jira). `drift` (optional text on a ticket, e.g. `"description changed in Jira since 2026-11-20"`) says its requirements changed and the plan wasn't regenerated: the report shows it and the ticket can't pass plainly; leave it out after regenerating the ACs and cases. In a ticket's folder (`qa-out/<KEY>/`) a re-run or retest runs in the same folder, and the earlier results stay in its run files: it needs no `retest_of` or `prior_passed`. Only a partial `--retest` that starts from an older dated folder (`qa-out/<date>-<keys>/`, made before one folder per ticket) sets optional `"retest_of": "<that folder's name>"` and `"prior_passed": [{"id", "ticket", "ac", "title", "route", "build"}]` for cases that passed on an earlier build and are not re-run here (the renderer flags them honestly as `PASS WITH NOTES` and notes them in the pass-comment draft's `CHECK BEFORE POSTING` block).

## Ticket and plan
- `acs`: only what the ticket states. A missing rule, an ambiguous word, an unclear role or state is a question: `Q1 [BLOCKING]`, `[HIGH]` or `[LOW]`. Done when every AC and question is numbered. With several tickets, write each question as an object with its `ticket`. While a `[BLOCKING]` one is open, that ticket (untagged: every ticket) gets `PASS WITH NOTES` at best; once answered, set `"status": "resolved"` and the `answer`.
- `plan`: scope by AC id; approach (levels, roles, pages, P1 first); risks with impact and covering cases; test_data (the tenant or site, roles, licenses, `qa-sdet-` records, flags: what a case needs before its first step).
- `out_of_scope`: checks only an API or unit test can prove (test at the cheapest level), and failures this run can't cause. Provoking an API error or an empty response needs `--faults`: write `Provoking a closures API error (needs --faults)`, not `API errors`; with `--faults` they are cases. It limits what the run causes, never what it judges: a failure the app shows on its own is still tested.

## Cases
- Ids TC-1..n per ticket folder (across the run in an older combined folder), unique, and never changed: a reused or reviewed plan keeps every id, a new case takes the next free number, a removed one leaves a gap.
- `ac`: `AC-1`, or every AC the expected result checks (`AC-1, AC-2`). `route`: where the case starts (empty when unknown).
- `priority`, exactly: **P1** AC core, money, safety, data loss; **P2** common variants; **P3** edge, cosmetic.
- `technique`: EP (one case per valid and per invalid class), BVA (a-1, a, a+1, b-1, b, b+1), decision table, state transition (each valid transition once, key invalid ones), pairwise (3+ independent params), negative (stale data, double submit, past bugs from `qa-mem bugs`).
- `steps`: one action each, concrete values, fields by label, setup first (`Save zone qa-sdet-zone-10 with speed 50`). One input value per case (131 and 1000 are two cases). No `if … otherwise`: a step this run can't do makes an out_of_scope item.
- `expected`: the visible result that means pass, with its oracle (AC, label, invariant). For a save, name what proves it: the confirmation, the value after a reload, another page that shows it. What only a later job proves (a schedule that runs tonight, an export built in the background) is an out_of_scope item unless the page shows its result. Dates, times and numbers as the UI shows them, not as the code stores them; no format in the AC: check the UI or ask. A case on a situation the AC leaves open says `See Q1`.
- Code is a hint, not a spec. `qa-mem code '<route>'` gives field labels and inputs; cite them as "(from code)". A code rule the AC doesn't state, or that differs (code max 120, AC 130), is a question: test with the AC's values and expected result.

## Retest from an older folder
Only for a ticket whose results sit in an older dated folder (before each ticket had its own): `plan_state.latest_run` names that folder, and `subset_of` means it is this ticket's part of a combined run, with its TC ids. Write `<run>/.data/plan.json` in the ticket's folder from the older `plan.json`: the new plan keeps only `latest_run.tickets` and their cases (another ticket of that run would render BLOCKED here), every `TC-n` id verbatim, resolved questions refreshed from the new Jira comments, and `"retest_of": "<the older folder's name>"`. Without `--all`, move `passed_cases` into `"prior_passed"` (keeping each case's prior `build`) and keep in `"cases"` exactly the prior plan's case objects (steps and expected included) whose ids are in `retest_cases` (an entry with a `case` object, a pass an earlier retest carried: that object): `FAIL`/`BLOCKED`/`NOT_RUN`/`QUESTION`, plus `PASS` cases from a run that ended `BLOCKED` or on the page of a CONFIRMED CRITICAL/HIGH bug no retested case names (`retest_cases` itself only names them). With `--all`, keep every case in `"cases"`. When `plan_state.drift` names the ticket, ask once whether to regenerate its ACs and cases, as the skill says. From then on the ticket is re-run in its own folder.
