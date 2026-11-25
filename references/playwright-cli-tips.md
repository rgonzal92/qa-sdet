# playwright-cli: cheap and fast

Every command starts `playwright-cli -s=qa <cmd>` (the allow rules match that shape). Start with `qa-mem browser-open '<url>'`, then move with `goto` (never `open`: it starts a new browser without the sign-in, and the guard denies it), end with `close`. `goto` takes a full URL (`qaBaseUrl` + path): the guard checks the host, and a bare path like `/form` has none, so it is denied. Commands print the page status and a snapshot file path; `find` searches it; read snapshot files with `grep` or `head`. `--raw` prints only the value.

| Need | Use | Avoid |
|---|---|---|
| Is X on the page? | `find 'Save'`, `find --regex '/error|invalid/i'` | a full `snapshot` |
| Act | a card locator directly: `click "getByRole('button', { name: 'Save' })"` | a snapshot just for a ref |
| Structure, refs | `snapshot --depth=4`, then `snapshot <ref>`; a locator to keep: `generate-locator <ref>` | whole-page snapshots every step |
| Silent failure | `console error`, `requests`, then `response-body <n>` / `request-body <n>` | `request <n>`, `response-headers <n>` (denied: cookies); `--clear` (it also empties the `--all` list your step 9 saves) |
| Read a value | `--raw eval "document.querySelector('#x').textContent"`; one header: `--raw eval "fetch(url).then(r => r.headers.get('content-type'))"` | snapshot + reading |
| Many input values | one `--raw eval` loop (below) | a fill and a read per value; `run-code` (denied) |
| Several steps | one Bash call: `playwright-cli -s=qa fill "<locator>" 131 && playwright-cli -s=qa click "<locator>"` (each still counts toward a budget) | one call per command |

Many values in one eval; repeat a suspicious one with real `fill` / `click` before calling it a bug (the recording needs them; its code goes into the repro spec):
```
playwright-cli -s=qa --raw eval "(async () => { const el = document.querySelector('[name=speed]'), out = {};
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;  // React inputs too
  for (const v of ['129', '131']) { set.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true}));
    el.form.requestSubmit(); await new Promise(r => setTimeout(r, 300)); out[v] = document.querySelector('[role=status]')?.textContent; }
  return JSON.stringify(out); })()"
```

## Bug video (the clean-state repro)
```
playwright-cli -s=qa recording-start && playwright-cli -s=qa video-start '<out>/evidence/BUG-<n>.webm'
playwright-cli -s=qa video-show-actions --cursor none --highlight-style 'outline: 3px solid red' --position top-right
playwright-cli -s=qa reload
playwright-cli -s=qa video-hide-actions; playwright-cli -s=qa video-stop && playwright-cli -s=qa recording-stop
```
- `video-show-actions` names each action in the video and boxes its element, so the developer sees what was clicked and typed; if it fails (an older playwright-cli), go on without it.
- Between `reload` (or a `goto` to the start page) and `video-stop`: the repro steps with real `fill` / `click`. n = the number this bug will get (the highest in `<out>/bugs/` + 1).
- The bug didn't show again (SUSPECTED): keep the video and say in the draft that it shows the attempt that didn't hit.
- Always `--cursor none` (the pointer slows every action by 800 ms). No videos for cases.

## Network evidence (a bug in an API call)
A call that failed, or that the server wrongly accepted: save what was sent and returned, while the browser is open, before step 9 (never for login, session, token or profile calls):
```
playwright-cli -s=qa requests --filter '<path regexp>' > '<out>/evidence/BUG-<n>-network.txt'; playwright-cli -s=qa request-body <#> >> '<out>/evidence/BUG-<n>-network.txt'; playwright-cli -s=qa response-body <#> >> '<out>/evidence/BUG-<n>-network.txt'
```
`<#>` is the call's number in `requests`. The file is scrubbed before the report links it and the filing command attaches it.

## Look for broken output (one call, read-only)
After the page loads, and after each submit or big state change, one `--raw eval` lists visible breakage the page doesn't report: `NaN`, `undefined`, `[object Object]` or `Invalid Date` in visible text, broken images, a page wider than the window, a region still busy, controls with no accessible name. Each hit is a lead: check it like any other result.
```
playwright-cli -s=qa --raw eval "(() => { const out = [], seen = el => el && el.offsetParent !== null;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) { const t = n.textContent.trim(); if (t && seen(n.parentElement) && !/^(SCRIPT|STYLE|TEXTAREA)$/.test(n.parentElement.tagName) && /\b(NaN|undefined|Invalid Date)\b|\[object Object\]/.test(t)) out.push('text: ' + t.slice(0, 80)); }
  for (const i of document.images) if (i.complete && !i.naturalWidth && seen(i)) out.push('broken image: ' + (i.alt || i.src).slice(0, 80));
  if (document.documentElement.scrollWidth > innerWidth + 2) out.push('wider than the window');
  for (const b of document.querySelectorAll('[aria-busy=true]')) if (seen(b)) out.push('still busy: ' + (b.id || b.tagName));
  for (const e of document.querySelectorAll('button, a[href], input:not([type=hidden])')) if (seen(e) && !(e.getAttribute('aria-label') || e.textContent.trim() || (e.labels && e.labels.length) || e.title)) out.push('no accessible name: ' + e.outerHTML.slice(0, 60));
  return out.slice(0, 20).join(' | ') || 'none'; })()"
```

## Evidence image (bug or case)
A bug: after `recording-stop` (its toolbar shows in screenshots). A case: before you cancel any dialog. One call, nothing else before it:
```
qa-mem evidence '<out>/evidence/<BUG-n or TC-n>.png' --selector '<css>' --label '<at most 80 chars>'
```
- It scrolls to the element, takes the viewport screenshot and draws a red box and the label next to it; it prints `{"path", "annotated", "method"}`. The element is the one that shows the problem (the wrong value, the error text, the bad row). Bug label: actual vs expected (`Saved at 131 km/h; max is 130`). Case label: `TC-<n> <STATUS>: <value you read>`.
- `method` `highlight`: Pillow is missing, so the browser drew the box without a label: put the label text in the draft's Actual or the case's `actual`.
- An element in a frame (the app shell loads pages in an iframe; a CSS selector can't reach inside): `find '<text on the element>'` shows its ref (`status [ref=f1e15]: Saved …`), then `qa-mem evidence '<png>' --ref f1e15 --label '<text>'`.
- `no element matches`: fix the selector, or use `--ref`. Never `--hires` or `--full-page`.

**When `qa-mem evidence` fails** (exit 1 on a browser command): take the image plain, `playwright-cli -s=qa screenshot --filename='<out>/evidence/<BUG-n or TC-n>.png'`, set `annotated: false` and name the element in the draft's Actual or the case's `actual`.
