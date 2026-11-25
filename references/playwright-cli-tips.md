# playwright-cli: cheap and fast

Every command starts `playwright-cli -s=qa <cmd>` (the allow rules match that shape). Start with `qa-mem browser-open '<url>'`, end with `close`. `goto` takes a full URL (`qaBaseUrl` + path): the guard checks the host, and a bare path like `/form` has none, so it is denied. Commands print the page status and a snapshot file path; `find` searches it; read snapshot files with `grep` or `head`. `--raw` prints only the value.

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
playwright-cli -s=qa reload
playwright-cli -s=qa video-stop && playwright-cli -s=qa recording-stop
```
- Between `reload` (or a `goto` to the start page) and `video-stop`: the repro steps with real `fill` / `click`. n = the number this bug will get (the highest in `<out>/bugs/` + 1).
- The bug didn't show again (SUSPECTED): keep the video and say in the draft that it shows the attempt that didn't hit.
- No `--cursor` (it slows every action by 800 ms). No videos for cases.

## Evidence image (bug or case)
A bug: after `recording-stop` (its toolbar shows in screenshots). A case: before you cancel any dialog. Run nothing between the eval and the screenshot.
```
playwright-cli -s=qa --raw eval "(() => { const el = document.querySelector('<css>'); el.scrollIntoView({block: 'center'});
  const r = el.getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map(Math.round).join(','); })()"
playwright-cli -s=qa screenshot --filename='<out>/evidence/<BUG-n or TC-n>.png'
qa-mem annotate '<out>/evidence/<BUG-n or TC-n>.png' --box <x,y,w,h> --label '<at most 80 chars>'
```
- `screenshot` saves the viewport in CSS pixels, the unit of `getBoundingClientRect()`: no `devicePixelRatio` factor, never `--hires` or `--full-page`. Always pass `--filename`.
- The box is the element that shows the problem (the wrong value, the error text, the bad row). A second `--box` is allowed. The label sits by the first box, so that is the element it describes. Bug label: actual vs expected (`Saved at 131 km/h; max is 130`). Case label: `TC-<n> <STATUS>: <value you read>`.
- `annotate` says Pillow is missing: take the image again now with the highlight steps below (same file), and use them for the rest of the run.

**Highlight variant:** for an element in a frame (the app shell loads pages in an iframe; the eval above and card locators can't reach inside), or when Pillow is missing. playwright-cli draws the box itself, also in frames from another origin. `find '<text on the element>'` shows its ref (`status [ref=f1e15]: Saved …`); then, with nothing else between the highlight and the screenshot:
```
playwright-cli -s=qa eval "el => el.scrollIntoView({block: 'center'})" <ref>
playwright-cli -s=qa highlight <ref> --style 'outline: 3px solid red'
playwright-cli -s=qa screenshot --filename='<out>/evidence/<BUG-n or TC-n>.png'
playwright-cli -s=qa highlight --hide
```
- The box has no label, only a small tag naming the ref: put the label text (actual vs expected) in the draft's Actual or the case's `actual`. `annotated: true`.
- `--hide` before the next screenshot, or the old box shows up in it.
