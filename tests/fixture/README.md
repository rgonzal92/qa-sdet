Fixture app for qa-sdet self-checks. Serve: `python3 tests/fixture/serve.py 8765` (the static pages, the shell's
`/console/<module>` routes, a `/graphql` endpoint and an in-memory `/api/notes`; `python3 -m http.server` serves only
the static pages). `make test-browser` runs `qa-mem` against it in a real browser and checks every plant below.

Planted bugs (answer key, keep out of prompts):
- `form.html`: speed accepts >130.
- `map.html`: the Closures layer's 404 is shown as "0 closures shown".
- `gql.html`: every call answers 200; `Zones` fails at the top level, `SaveNote` inside its payload while the page says
  "Saved", `BulkUpdate` reports 1 failed of 3 while the page says "Updated 2 notes".
- `grid.html`: a filter typed on page 4 keeps the page index ("No detectors" with 9 matches).
- `dashboard.html`: a filter on Signals changes the Summary's total (`Active: 8 (800%)`).
- `timefield.html`: the first minute digit typed empties the hour (`fill` hides it); Escape in the preview closes the
  edit dialog too.
- `export.html`: the weekly report's tab stays blank and the page says nothing.
- `notes.html`: an edited priority says "Saved" and is gone after a reload; a refused name closes the dialog with
  "Something went wrong" and drops the text; a name longer than 60 characters answers 502 but the note is stored.
- `leaks-frame.html` (inside `leaks.html`): a raw ISO timestamp, an internal state name, 800% and an icon button with no
  name.

Correct on purpose (not bugs): `dashboard.html`'s failed Alarms panel says so and offers Retry; `notes.html`'s delete
asks for the word `delete`; an edit refused by the server keeps its dialog and its reason; the shell (`/console/…`)
keeps every module's frame mounted and shows one; a module opened outside the shell refuses to load.
