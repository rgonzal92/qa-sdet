# run-<slug>.json (one per explored page: feeds the memory DB and the report)

Write it to `<out>/.data/run-<slug>.json` (`<out>` = the run folder, `<slug>` = the route slug from the caller) as the last step. The plugin's Stop/SubagentStop hook ingests it
automatically; it is the only way knowledge gets into the DB (the user's `bug-status` / `stale` only change statuses). Rewrite the file to update it; changes are re-ingested. Routes are normalised (scheme/host/query/trailing `/` dropped; `#/...` and `#!/...` SPA routes kept; numeric, UUID and long-hex segments become `:id`, so `/incidents/4521` and `/incidents/77` share one card). A file with wrong types is parked as `run-<slug>.json.error` and the hook shows you the error once when you finish: fix the file and rewrite it. `qa-mem report` renders REPORT.md from `cases`, `questions`, `not_tested`, `notes` and `bugs`.

```json
{
  "run_id": "ticket-form-20261122-1530",
  "mode": "explore | ticket | plan | map",
  "target": "/form (the route this run explored)",
  "build": "1.42.0",
  "status": "COMPLETED | INCOMPLETE | BLOCKED",
  "summary": "one line, ≤300 chars",
  "env": {"url": "<target URL>", "browser": "<config browser, or mcp>"},
  "cases": [{"id": "TC-1", "status": "PASS | FAIL | BLOCKED | NOT_RUN | QUESTION",
             "actual": "what you saw, ≤300 chars", "notes": "≤300 chars", "bug": "BUG-2 or empty"}],
  "questions": [{"text": "for the product owner or dev, ≤300 chars", "status": "open | resolved", "answer": "only when resolved"}],
  "not_tested": [{"category": "safety | out_of_scope | not_reached | needs_setup | heuristic | other", "text": "what and why, ≤300 chars"}],
  "notes": "what you explored, in plain words, ≤1500 chars",
  "pages": [{
    "route": "/form",
    "name": "Signal timing form",
    "facts": [{"kind": "rule|flow|api|validation|note", "text": "≤300 chars, verified", "source": "observed|AC|STUDIO-1234"}],
    "locators": [{"name": "Save button", "locator": "getByRole('button', { name: 'Save' })"}],
    "coverage": [{"target": "Speed limit field", "heuristic": "boundary", "result": "pass|fail|blocked"}]
  }],
  "bugs": [{"route": "/form", "title": "…", "symptom": "short symptom, used for dedup",
            "hash": "only when re-sighting a KNOWN BUG from the card: its 12-char hash",
            "severity": "HIGH", "classification": "CONFIRMED", "draft_path": "<absolute path of bugs/BUG-n.md>",
            "evidence": "<absolute path of evidence/BUG-n.png>", "annotated": true, "ticket": "STUDIO-1234"}]
}
```

Rules
- `target` = the route explored, never a ticket key: ticket keys go in `cases` (via their ids) and `bugs[].ticket`.
- `build` = exactly the text read via config buildSource, or `unknown` when unreadable; no notes (any other text is a different build, which marks everything stale).
- `run_id` = `<mode>-<slug>-<UTC yyyymmdd-hhmm>` (`date -u +%Y%m%d-%H%M`); a new run gets a new id (seen_count counts distinct run ids).
- `cases`: one entry per case the caller gave, ids exactly as given; a case you never reached (budget or time ran out) is `NOT_RUN` with the reason in `notes`; one you reached but couldn't do is `BLOCKED`. A FAIL names its bug in `bug`: `BUG-<n>` of its draft, a KNOWN BUG's hash, or a known Jira key. Leave `cases` empty when you were given none.
- `not_tested`: pick the category that fits. `safety` = held back on purpose by the run's rules; `out_of_scope` = outside this ticket; `not_reached` = budget, time or route ran out; `needs_setup` = needs a fault, account or data this run lacks; `heuristic` = a heuristic from the card's UNTRIED line that was not run; `other` = anything else. A plain string still works and is shown under Other.
- Case images (ticket mode): for each case with a result, take one image while its checked value is on screen: `<out>/evidence/TC-<n>.png`. The report embeds it when the file exists. Never a new `run-*.json` for images.
- Facts are things you **verified** (observed, or stated in a ticket). Guesses go in `questions`, not facts.
- Locators: only ones you used successfully this run (role/label/test-id form).
- `heuristic` must be an ID from heuristics.md; anything else is stored as `other:<text>` and doesn't count as covering a heuristic.
- Every bug needs `title` and `route`; a new bug also needs the same `symptom` you hashed in its draft (without it the DB hashes the title, and the draft's hash won't match). Same bug as one in the card's KNOWN BUGS or PREVIOUSLY FIXED → include its `hash` (counts a sighting). A KNOWN BUG gets no new draft; a PREVIOUSLY FIXED one that is back gets a short draft saying it regressed. New bug → no hash; it is computed from route + symptom (`qa-mem hash` shows it).
- `ticket` on a bug: the ticket whose case or AC found it; leave it out for bugs found while exploring around.
