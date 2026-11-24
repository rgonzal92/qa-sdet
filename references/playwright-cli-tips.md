# playwright-cli: cheap and fast

Every command starts `playwright-cli -s=qa <cmd>` (the allow rules match that shape). Start with `qa-mem browser-open '<url>'`, end with `close`. `goto` takes a full URL (`qaBaseUrl` + path): the guard checks the host, and a bare path like `/form` has none, so it is denied. Commands print the page status and a snapshot file path; `find` searches it; read snapshot files with `grep` or `head`. `--raw` prints only the value.

| Need | Use | Avoid |
|---|---|---|
| Is X on the page? | `find 'Save'`, `find --regex '/error|invalid/i'` | a full `snapshot` |
| Act | a card locator directly: `click "getByRole('button', { name: 'Save' })"` | a snapshot just for a ref |
| Structure, refs | `snapshot --depth=4`, then `snapshot <ref>`; a locator to keep: `generate-locator <ref>` | whole-page snapshots every step |
| Silent failure | `console error`, `requests`, then `response-body <n>` / `request-body <n>` | `request <n>`, `response-headers <n>` (denied: cookies) |
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
- `annotate` says Pillow is missing: keep the image, describe the spot in the draft, `annotated: false`.
- **In-frame variant** (the app shell loads pages in an iframe, which the eval above can't see). Find the frame once: `playwright-cli -s=qa --raw eval "[...document.querySelectorAll('iframe')].map(f => f.id ? '#' + f.id : '[name=' + f.name + ']').join(' ')"`. Then the eval adds the frame's offset:
```
playwright-cli -s=qa --raw eval "(() => { const f = document.querySelector('iframe<selector>');
  const el = f.contentDocument.querySelector('<css>'); el.scrollIntoView({block: 'center'});
  const o = f.getBoundingClientRect(), r = el.getBoundingClientRect();
  return [o.x + f.clientLeft + r.x, o.y + f.clientTop + r.y, r.width, r.height].map(Math.round).join(','); })()"
```
  A frame on another origin has no `contentDocument`: keep the plain screenshot. Not checked on the live app yet: keep the plain screenshot if the box looks wrong.
