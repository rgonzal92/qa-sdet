# What goes in a test plan (the content of plan.json)

The fields and their JSON shape are in report.md. This is what to put in them.

## Per ticket
- **acs**: the acceptance criteria as the ticket states them, AC-1..n. Nothing invented: a missing rule is a question.
- **plan.scope**: what is tested, mapped to AC ids (3–6 short items in one string, `; `-separated).
- **plan.approach**: techniques and levels (UI, API), which roles and pages, the order (P1 first).
- **plan.risks**: what could go wrong for users or data, each with its impact and the case(s) covering it (`Wrong limit saved: data error (TC-3, TC-4)`).
- **plan.out_of_scope**: checks not written as cases, and why: only an API or unit test can prove them, or this run can't cause them (provoking an API error or an empty response needs `--faults`: write `Provoking a closures API error (needs --faults)`, not `API errors`). It limits what the run causes, never what it judges: a failure the app shows on its own is still tested and failed. With `--faults`, such failures of an AC's call are cases, not out of scope.
- **plan.test_data**: roles/accounts, seed records (`qa-sdet-` names), feature flags, dependencies.

## Questions (top-level `questions`)
Ambiguous words, missing rules, roles and states, numbered and tagged: `Q1 [BLOCKING] …`, `Q2 [HIGH] …`, `Q3 [LOW] …`.

## Cases
| Field | Content |
|---|---|
| id | TC-1..n across the run. Once written, an id never changes: a reviewed or reused plan keeps every id, a new case takes the next free number, a removed one leaves a gap |
| ticket, ac | the AC it proves: one id (`AC-1`), or every AC its expected result checks (`AC-1, AC-2`); never an expected result that checks an AC `ac` leaves out |
| title | the check in a few words (`Speed 131 is rejected`) |
| route | the page where it starts |
| priority | exactly `P1`, `P2` or `P3`: P1 = AC core / money / safety / data loss, P2 = common variants, P3 = edge / cosmetic |
| technique | EP, BVA, decision table, state transition, negative (test-design.md) |
| steps | one action per item, concrete values (`Type 131 in Speed limit`), fields by their label (from `qa-mem code '<route>'` when mapped). Setup comes first as steps (`Save zone qa-sdet-zone-10 with speed 50`). One input value per case (131 and 1000 are two cases, so a FAIL names the value). No `if … otherwise` branches: a step this run can't do (needs `--faults`, data or a role you can't get) makes it an out_of_scope item, not a case |
| expected | the visible result that means pass, exact enough to decide PASS or FAIL (the text or value when the AC or a label gives it), with its oracle (AC, label, invariant) |

Where cases come from: the AC (its values, ranges, "only", "any other value" give the boundary and negative cases), the code map (`qa-mem code '<route>'`: field labels, rules such as min/max or required, endpoints) and past bugs. The expected result comes from the AC, a label or an invariant, never from code: a code rule the AC doesn't state, or one that differs from it (code max 120, AC 130), is a question; test it with the AC's values and expected result. A case for a situation the AC leaves open (an empty field) says in expected which question it rests on (`See Q1`).

Exit: all P1 cases run, no open HIGH+ bugs, questions answered or accepted.
