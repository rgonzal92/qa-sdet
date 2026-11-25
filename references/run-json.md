# run-<slug>.json

Your last file: `<out>/.data/run-<slug>.json`. The plugin's hook ingests it (the only way knowledge reaches the memory) and `qa-mem report` renders from it. Rewrite it to update. A wrong type parks it as `.error`: the hook tells you once; fix it and rewrite. Text fields: at most 300 characters.

```json
{
  "run_id": "ticket-form-20261122-1530",
  "mode": "explore | ticket | map",
  "target": "/form",
  "build": "1.42.0",
  "status": "COMPLETED | INCOMPLETE | BLOCKED",
  "summary": "one line",
  "env": {"url": "<target URL>", "browser": "<config browser>"},
  "cases": [{"id": "TC-1", "status": "PASS | FAIL | BLOCKED | NOT_RUN | QUESTION",
             "actual": "what you saw", "notes": "", "bug": "BUG-2 or empty", "annotated": true}],
  "questions": [{"text": "for the product owner or a dev", "status": "open | resolved", "answer": "when resolved"}],
  "not_tested": [{"category": "safety | out_of_scope | not_reached | needs_setup | heuristic | other", "text": "what and why"}],
  "notes": "what you explored, in plain words (at most 1500 chars)",
  "pages": [{
    "route": "/form", "name": "Signal timing form",
    "facts": [{"kind": "rule|flow|api|validation|note", "text": "verified", "source": "observed|AC|PROJ-1234"}],
    "locators": [{"name": "Save button", "locator": "getByRole('button', { name: 'Save' })"}],
    "coverage": [{"target": "Speed limit field", "heuristic": "boundary", "result": "pass|fail|blocked"}]
  }],
  "bugs": [{"route": "/form", "title": "…", "symptom": "short, used for dedup", "hash": "only for a re-sighting",
            "severity": "HIGH", "classification": "CONFIRMED", "draft_path": "<absolute bugs/BUG-n.md>",
            "evidence": "<absolute evidence/BUG-n.png>", "annotated": true, "ticket": "PROJ-1234"}]
}
```
- `target`: the route explored, never a ticket key. `build`: exactly the text read via `buildSource`, or `unknown` (other text counts as a new build). `run_id`: `<mode>-<slug>-<UTC yyyymmdd-hhmm>`, new for each run.
- `cases`: one per given case, ids as given; none given: empty. NOT_RUN (the budget or time ran out first) says why in `notes`; one you reached but couldn't do is `BLOCKED`. A FAIL names its bug in `bug`: `BUG-<n>`, a KNOWN BUG's hash, or a Jira key.
- `not_tested.category`: `safety` held back by the run's rules; `out_of_scope` outside this ticket; `not_reached` budget, time or route ran out; `needs_setup` needs a fault, account or data this run lacks; `heuristic` an UNTRIED heuristic you skipped; `other`.
- Facts are verified (seen, or stated in a ticket); guesses are `questions`. Locators: only ones that worked, in role, label or test-id form.
- `coverage[].heuristic`: an ID from the card's UNTRIED or COVERED lists (`heuristics.md` in explore mode); anything else is stored as `other:<text>` and covers nothing, except `regression` (target = a PREVIOUSLY FIXED bug's hash): the card then marks that bug re-checked on this build. `coverage[].target`: a check on a field the card names under UNTESTED TARGETS or FROM CODE rules uses that label as written (the card matches it to the code rule).
- A bug needs `title` and `route`. A new bug has no `hash` and the same `symptom` you hashed in its draft. A re-sighting of a KNOWN BUG, a PREVIOUSLY FIXED one or one from BUGS ON PAGES SHARING AN ENDPOINT carries its `hash`, the severity its card line shows (`[HIGH]` is `HIGH`) and `classification` `CONFIRMED` (seen again is a reproduction): the report rates it like a new bug. `ticket`: the ticket whose case or AC found it; leave it out for bugs found around.
