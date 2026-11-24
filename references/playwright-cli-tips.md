# playwright-cli: cheap and fast

Always use the one named session `playwright-cli -s=qa <cmd>`, so the browser stays open between commands.

| Need | Use | Avoid |
|---|---|---|
| Is X on the page? | `find 'Save'` / `find --regex '/error|invalid/i'` | full `snapshot` |
| Act on element | cached locator from card directly: `click "getByRole('button', { name: 'Save' })"` | snapshot just to get a ref |
| Need refs | `snapshot --depth=4`, then `snapshot <ref>` for the region | whole-page deep snapshot every step |
| Stable locator to save | `generate-locator <ref>` | hand-written CSS |
| Did it fail silently? | `console error`, `requests`, then `response-body <n>` / `request-body <n>` for one call; one header: `--raw eval "fetch(url).then(r => r.headers.get('content-type'))"` | screenshots, `request <n>` / `response-headers <n>` (blocked: they print cookies) |
| Bug evidence | box recipe below, `screenshot --filename='<out>/evidence/BUG-<n>.png'`, then `qa-mem annotate` (always pass `--filename`: the default location is `.playwright-cli/`) | screenshot per step |
| See canvas/map | `screenshot --filename='<out>/.data/map-<n>.png'` | screenshots in `evidence/` (bug images only) |
| Repro script | `recording-start` … `recording-stop` → paste into repro/*.spec.ts | rewriting steps by hand |
| Read a value | `eval "document.querySelector('#x').textContent"` | snapshot + reading |
| Many input values | one `--raw eval` loop in the page (recipe below) | one fill + one read command per value; `run-code` (blocked: it runs Node) |
| Several steps in a row | one Bash call: `playwright-cli -s=qa fill "<locator>" 131 && playwright-cli -s=qa click "<locator>" && playwright-cli -s=qa find 'Saved'` (each command still counts toward a budget; fewer turns make the run faster and cheaper) | one Bash call per command |
| Fault injection | `route <pattern>`, `network-state-set offline` (needs --faults) | |

Session start: `qa-mem browser-open '<url>'` (opens `-s=qa` in the signed-in `main` profile with the config browser; it counts as one budgeted command).
Keep `-s=qa` as the first argument of every command: the user's allow rules match that shape.
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

## Box for a bug image (one eval, then screenshot, then annotate)
```
playwright-cli -s=qa --raw eval "(() => { const r = document.querySelector('<css of the element>').getBoundingClientRect();
  return [r.x, r.y, r.width, r.height].map(Math.round).join(','); })()"
playwright-cli -s=qa screenshot --filename='<out>/evidence/BUG-<n>.png'
qa-mem annotate '<out>/evidence/BUG-<n>.png' --box <x,y,w,h> --label '<what is wrong, ≤80 chars>'
```
`screenshot` saves the viewport in CSS pixels (no `--hires`, no `--full-page`), the same units as `getBoundingClientRect()`, so the box needs no `devicePixelRatio` factor.
Pick the element that shows the problem (the field with the wrong value, the error text, the bad row). Scroll it into view before the eval (`scrollIntoView({block: 'center'})` in the same eval works), and run nothing between the eval and the screenshot.
