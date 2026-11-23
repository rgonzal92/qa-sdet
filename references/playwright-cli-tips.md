# playwright-cli: cheap and fast

Always use the one named session `playwright-cli -s=qa <cmd>`, so the browser stays open between commands.

| Need | Use | Avoid |
|---|---|---|
| Is X on the page? | `find "Save"` / `find --regex "/error|invalid/i"` | full `snapshot` |
| Act on element | cached locator from card directly: `click "getByRole('button', { name: 'Save' })"` | snapshot just to get a ref |
| Need refs | `snapshot --depth=4`, then `snapshot <ref>` for the region | whole-page deep snapshot every step |
| Stable locator to save | `generate-locator <ref>` | hand-written CSS |
| Did it fail silently? | `console error`, `requests`, then `response-body <n>` / `request-body <n>` for one call; one header: `--raw eval "fetch(url).then(r => r.headers.get('content-type'))"` | screenshots, `request <n>` / `response-headers <n>` (blocked: they print cookies) |
| See canvas/map, bug evidence | `screenshot --filename=<out>/evidence/BUG-1-speed.png` (default location is `.playwright-cli/`, so always pass `--filename`) | screenshot per step |
| Repro script | `recording-start` … `recording-stop` → paste into repro/*.spec.ts | rewriting steps by hand |
| Read a value | `eval "document.querySelector('#x').textContent"` | snapshot + reading |
| Many input values | one `--raw eval` loop in the page (recipe below) | one fill + one read command per value; `run-code` (blocked: it runs Node) |
| Fault injection | `route <pattern>`, `network-state-set offline` (needs --faults) | |

Session start: `env -u PLAYWRIGHT_MCP_STORAGE_STATE playwright-cli -s=qa open --browser=<config browser>` → `state-load ~/.config/qa-sdet/qa-state.json` → `goto <url>`.
`goto` takes a full URL (`qaBaseUrl` + path); relative paths are denied, so build the URL from the config.
Session end: `playwright-cli -s=qa close`.

Every command prints page status + a link to a snapshot file in `.playwright-cli/`; it does not inline the tree. `find` searches that snapshot. Read snapshot files with `grep`/`head` (they can be large). Add `--raw` when you only need a value (e.g. `--raw eval ...`).

## Batch input values in one eval (runs in the page sandbox)
```
playwright-cli -s=qa --raw eval "(async () => { const el = document.querySelector('[name=speed]'); const out = {};
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;  // works for React/Angular inputs
  for (const v of ['129', '130', '131', '999']) { set.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true}));
    el.form.requestSubmit(); await new Promise(r => setTimeout(r, 300));
    out[v] = document.querySelector('[role=status]')?.textContent; }
  return JSON.stringify(out); })()"
```
Reproduce a suspicious value with real `fill`/`click` commands before calling it a bug: the recording and the repro spec need them.
