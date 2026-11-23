# run.json (how knowledge reaches the memory DB)

Write it to `<out>/run.json` (under the project's `qa-out/`) as the last step. The plugin's Stop/SubagentStop hook ingests it
automatically; it is the only way knowledge gets into the DB (the user's `bug-status` / `stale` only change statuses). Rewrite the file to update it; changes are re-ingested. Routes are normalised (scheme/host/query/trailing `/` dropped; `#/...` and `#!/...` SPA routes kept; numeric, UUID and long-hex segments become `:id`, so `/incidents/4521` and `/incidents/77` share one card). A file with wrong types is parked as `run.json.error` and the hook shows you the error once when you finish: fix the file and rewrite it.

```json
{
  "run_id": "explore-form-20261122-1530",
  "mode": "explore | ticket | plan",
  "target": "/form or PROJ-123",
  "build": "1.42.0",
  "status": "COMPLETED | INCOMPLETE | BLOCKED",
  "summary": "one line, ≤300 chars",
  "pages": [{
    "route": "/form",
    "name": "Signal timing form",
    "facts": [{"kind": "rule|flow|api|validation|note", "text": "≤300 chars, verified", "source": "observed|AC|PROJ-123"}],
    "locators": [{"name": "Save button", "locator": "getByRole('button', { name: 'Save' })"}],
    "coverage": [{"target": "Speed limit field", "heuristic": "boundary", "result": "pass|fail|blocked"}]
  }],
  "bugs": [{"route": "/form", "title": "…", "symptom": "short symptom, used for dedup",
            "hash": "only when re-sighting a KNOWN BUG from the card: its 12-char hash",
            "severity": "HIGH", "classification": "CONFIRMED", "draft_path": "<absolute path of the draft>"}]
}
```

Rules
- `build` = exactly the text read via config buildSource, or `unknown` when unreadable; no notes (any other text is a different build, which marks everything stale).
- `run_id` = `<mode>-<out folder name>-<UTC yyyymmdd-hhmm>` (`date -u +%Y%m%d-%H%M`); a new run gets a new id (seen_count counts distinct run ids, so two pages of one ticket must differ by folder name).
- Facts are things you **verified** (observed, or stated in a ticket). Guesses go in the report as questions, not facts.
- Locators: only ones you used successfully this run (role/label/test-id form).
- `heuristic` must be an ID from heuristics.md; anything else is stored as `other:<text>` and doesn't count as covering a heuristic.
- Every bug needs `title` and `route`; a new bug also needs the same `symptom` you hashed in its draft (without it the DB hashes the title, and the draft's hash won't match). Same bug as one in the card's KNOWN BUGS or PREVIOUSLY FIXED → include its `hash` (counts a sighting, no new draft). New bug → no hash; it is computed from route + symptom (`qa-mem hash` shows it).
