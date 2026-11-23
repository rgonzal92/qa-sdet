# Playwright MCP: the fallback browser

Used when the caller passes `browser=mcp:<prefix>` (playwright-cli is missing or broken). Tool names below are
`<prefix>browser_…`, e.g. `mcp__playwright__browser_navigate`. Every procedure step still applies; only the tool changes.
If the tools are listed by name only (deferred), load the ones you need in one ToolSearch call first:
`select:<prefix>browser_tabs,<prefix>browser_navigate,<prefix>browser_snapshot,<prefix>browser_find,<prefix>browser_click,<prefix>browser_type,<prefix>browser_fill_form,<prefix>browser_evaluate,<prefix>browser_console_messages,<prefix>browser_network_requests,<prefix>browser_network_request,<prefix>browser_take_screenshot`.
The guard counts each `browser_*` call against the run budget (closing is free) and applies the same host,
secret and file rules as for playwright-cli.

| Procedure step / CLI command | MCP tool |
|---|---|
| 1. Start: `open` + `state-load` + `goto <target>` | `browser_tabs` `{action: "new", url: <target>}`: your own tab (no open or state-load: the MCP's browser holds the login; the caller's sign-in tab keeps the login request in its network log, so leave that tab alone) |
| `find "Save"` | `browser_find` `{text: "Save"}` or `{regex: "error|invalid"}`; older versions without it: `browser_snapshot` with `filename`, then `grep` the file |
| `snapshot --depth=4` | `browser_snapshot` `{depth: 4}` (`depth` only where the schema lists it); pass `filename` `<out>/evidence/snapshot.md` for big pages and grep it |
| `click`, `fill`, `type`, `select`, `hover`, `press` | `browser_click`, `browser_type`, `browser_fill_form`, `browser_select_option`, `browser_hover`, `browser_press_key` with the `target` ref from the snapshot |
| 3. Build, `--raw eval "<js>"`, batched input values | `browser_evaluate` `{function: "() => <js>"}` (async functions work: `async () => {…}`) |
| `console error` | `browser_console_messages` `{level: "error"}` |
| `requests`, `response-body <n>`, `request-body <n>` | `browser_network_requests`, then `browser_network_request` `{index: n, part: "response-body"}` (or `request-body`) |
| one header | `browser_evaluate` `"() => fetch(location.href).then(r => r.headers.get('<name>'))"` |
| `screenshot --filename=<out>/evidence/BUG-n-x.png` | `browser_take_screenshot` `{filename: "<out>/evidence/BUG-n-x.png"}` |
| new tab | `browser_tabs` `{action: "new", url: …}`. Switching tabs (`select`) is blocked. A link that opens a new tab: read its `href` and `browser_navigate` to it in your tab. A popup: `browser_tabs` `{action: "list"}`, then `{action: "close", index: <the popup's n>}` (without an index, close shuts the current tab: yours) |
| 9. `close` | `browser_tabs` `{action: "list"}`, then `{action: "close", index: <your tab's n>}`; `browser_close` is blocked during runs (it would end the login) |

Blocked, as with the CLI: `browser_run_code_unsafe` (runs in the server, not the page), `browser_network_request`
without a body `part` (headers carry cookies), cookie/storage list and get tools, tracing, `browser_close`, evaluate code that reads cookie or storage values.

What the MCP can't do, and what to do instead:
- **Recording:** there is no `recording-start`. Write the repro spec by hand from the exact steps you ran, with
  role/label/test-id locators taken from the snapshot.
- **Faults:** only when the MCP exposes `browser_route` / `browser_network_state_set` (e.g. `--caps=network`) and the run has `--faults`; otherwise record fault heuristics as not run, reason "no fault tools in MCP".
- **Browser not installed:** when `browser_navigate` fails saying the browser or a revision is missing, go to step 9 with status `BLOCKED`, summary `MCP browser not installed: install the browser the error names for that MCP (e.g. npx playwright install chromium)`, quoting the missing path from the error. That is the user's machine to change.
- **Login:** an SSO page after `browser_navigate` means the MCP browser isn't signed in: step 9 with status `BLOCKED`,
  summary `MCP browser not signed in: sign in to the QA app in that browser (or install playwright-cli and run /qa-sdet:qa-login)`.
