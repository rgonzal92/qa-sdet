Fixture app for qa-sdet self-checks. Serve: `python3 tests/fixture/serve.py 8765` (the static pages, the shell's
`/console/<module>` routes and a `/graphql` endpoint; `python3 -m http.server` serves only the static pages).
`make test-browser` runs `qa-mem` against it in a real browser.
Planted bugs (answer key, keep out of prompts): form.html speed accepts >130; map.html Closures layer 404 shown as
"0 closures shown".
