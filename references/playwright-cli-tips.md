# playwright-cli: cheap and fast

Every command starts `playwright-cli -s=qa <cmd>` (the allow rules match that shape). Step 1's `qa-mem page-start` opened the browser: move with `goto` (never `open` or `qa-mem browser-open`: each restarts the browser and drops the request and console history step 9 saves), end with `close`. `goto` takes a full URL (`qaBaseUrl` + path): the guard checks the host, and a bare path like `/form` has none, so it is denied. Commands print the page status and a snapshot file path; `find` searches it; read snapshot files with `grep` or `head`. `--raw` prints only the value.

| Need | Use | Avoid |
|---|---|---|
| Is X on the page? | `find 'Save'`, `find --regex '/error|invalid/i'` | a full `snapshot` |
| Act | a card locator directly: `click "getByRole('button', { name: 'Save' })"` | a snapshot just for a ref |
| Structure, refs | `snapshot --depth=4`, then `snapshot <ref>`; a locator to keep: `generate-locator <ref>` | whole-page snapshots every step |
| Silent failure | `console error`, `requests`, then `response-body <n>` / `request-body <n>` | `request <n>`, `response-headers <n>` (denied: cookies); `--clear` (it also empties the `--all` list your step 9 saves) |
| Read a value | `--raw eval "document.querySelector('#x').textContent"`; one header: `--raw eval "fetch(url).then(r => r.headers.get('content-type'))"` | snapshot + reading |
| Many input values | one `--raw eval` loop (below) | a fill and a read per value; `run-code` (denied) |
| Several steps | one Bash call: `playwright-cli -s=qa fill "<locator>" 131 && playwright-cli -s=qa click "<locator>"`; a throwaway file in `<out>/scratch/` | one call per command; a variable, `$(…)`, a loop or `jq` in the call (it then waits about 5 s for a permission check) |

Many values in one eval; repeat a suspicious one with real `fill` / `click` before calling it a bug (the recording needs them; its code goes into the repro spec):
```
playwright-cli -s=qa --raw eval "(async () => { const el = document.querySelector('[name=speed]'), out = {};
  const proto = {INPUT: HTMLInputElement, TEXTAREA: HTMLTextAreaElement, SELECT: HTMLSelectElement}[el.tagName].prototype;
  const set = Object.getOwnPropertyDescriptor(proto, 'value').set;  // the native setter of this tag: React inputs too
  const status = () => document.querySelector('[role=status]')?.textContent;
  for (const v of ['129', '131']) { const before = status(); set.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true}));
    if (!el.form.checkValidity()) { const bad = el.form.querySelector(':invalid'); out[v] = 'blocked by the browser: ' + bad.name + ' ' + bad.validationMessage; continue; }  // never submitted
    el.form.requestSubmit();
    for (let i = 0; i < 20 && status() === before; i++) await new Promise(r => setTimeout(r, 100));  // up to 2 s for the answer
    out[v] = status() + (status() === before ? ' (unchanged: the earlier value\'s text, or none)' : ''); }
  return JSON.stringify(out); })()"
```
**A page inside a frame** (an app shell): every eval in this file reads the top document, so give it a frame ref (from `find`) as its target and wrap the script: `--raw eval "el => (<the script>)" <ref>`.

A value the browser blocks (`min`, `max`, `maxlength`, `required`) is reported as blocked, not as the previous value's result; one that never changes the status text shows as unchanged: repeat it with real `fill` / `click`.

## Bug video (the clean-state repro)
```
playwright-cli -s=qa recording-start && playwright-cli -s=qa video-start '<out>/evidence/BUG-<n>.webm'
playwright-cli -s=qa video-show-actions --highlight-style 'outline: 3px solid red' --position top-right
playwright-cli -s=qa reload
playwright-cli -s=qa video-hide-actions; playwright-cli -s=qa video-stop && playwright-cli -s=qa recording-stop
```
- `video-show-actions` names each action in the video and boxes its element, so the developer sees what was clicked and typed; if it fails (an older playwright-cli), go on without it.
- Between `reload` (or a `goto` to the start page) and `video-stop`: the repro steps with real `fill` / `click`. n = the number this bug will get (the highest in `<out>/bugs/` + 1).
- The bug didn't show again (SUSPECTED): keep the video and say in the draft that it shows the attempt that didn't hit.
- No `--cursor` option: `--cursor none` fails on 0.1.24 (`too many arguments`; `--no-cursor` fails too), so the pointer animation stays and slows each action by about 800 ms: keep the repro to a few steps. No videos for cases.

## Network evidence (a bug in an API call)
A call that failed, or that the server wrongly accepted: save what was sent and returned, while the browser is open, before step 9 (never for login, session, token or profile calls):
```
playwright-cli -s=qa requests --filter '<path regexp>' > '<out>/evidence/BUG-<n>-network.txt'; playwright-cli -s=qa request-body <#> >> '<out>/evidence/BUG-<n>-network.txt'; playwright-cli -s=qa response-body <#> >> '<out>/evidence/BUG-<n>-network.txt'
```
`<#>` is the call's number in `requests`. The file is scrubbed before the report links it and the filing command attaches it.

## Error bodies: disguised failures and leaks (step 9, or when a call looks wrong)
The `requests` list shows only a status, so a 200 can hide a failure. Step 9's `qa-mem body-check '<out>' --slug <slug> --all` reads the bodies for you while the browser is open: every GraphQL and RPC call, every write, every failed call, at most 30 a run (`left`: run it again), never a sign-in call. It saves the request log itself, so it is one command at any time; the browser drops a call's answer when the page navigates, so run it right after a write, before the reload (an answer read too late is `no body left`: not judged, never clean). A GraphQL endpoint answers every operation at one method and path, so it names each call by its operation, `POST /graphql#SaveNote`: use that name in `notes`, a draft and `endpoint`. `qa-mem signals-triage` then names what is still unread (SUSPECT 200). One body by hand, never for login, session, token or profile calls:
```
playwright-cli -s=qa response-body <#> | qa-mem body-check '<out>' --slug <slug> <#>
```
It reads the body from stdin and prints what is wrong with it; the verdict, clean or not, goes into the run folder, so triage shows a bad body as a signal until a bug draft or `notes` names the call (`POST /graphql`). A clean answer clears the call.
- **A 2xx that is a failure:** a JSON body with `error` or `errors` (GraphQL: `{"errors": [{"message": …}]}`), `success` or `ok` false, or `status` `error`; a GraphQL mutation's own failure, `{"data": {"saveNote": {"note": null, "errors": […]}}}` or a non-empty `userErrors`; or an HTML page (a login redirect, a proxy page) on an API call.
- **A partial failure** (writes only): a batch answer with `failed: 1` or an item with `ok` false. Oracle: the page must name what failed, not show "Updated" for all of it; reload and check each row. Oracle: a call the page makes for its data or action either worked or did not; the page must not show "Saved" or "0 items" for it.
- **A leak, in any status:** a stack trace (`Traceback`, `at … (/srv/app/zones.js:42:11)`, `NullPointerException`), a SQL error (`SQLSTATE`, `ORA-00942`, `syntax error at or near`), or a framework debug page (`Whitelabel Error Page`, Django `DEBUG = True`, `Server Error in '/' Application`). Never shown to a user: a bug (severity HIGH when it names internals such as tables or server paths).
- **A 5xx on bad input:** a value the form or the server should refuse (past a limit, empty, the wrong type) must give a 4xx with a message. A 500 for it is a bug (boundary / invalid-format / empty-null), whatever the page shows.
- A 4xx whose body says so (`{"error": "…"}`) is the right answer; only its leaks count.

## Map the page's states and links (one call, read-only)
Once after the page loads: its same-host links and the triggers of in-page states (tabs, dialogs, menus, expanders), for the run JSON's `states` and `transitions`. Open the ones the card lists under UNVISITED STATES; never follow logout or delete links. A link's query string is dropped: `/list?page=2` and `/list?sort=name` are the page `/list` (a query view that shows other content is an in-page state, not a page). A hash that is a route is kept (`/#/zones` and `/#/zones/12` are two pages); a plain anchor (`#top`) is the page itself.
```
playwright-cli -s=qa --raw eval "(() => { const out = {links: [], states: []}, seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) { try { const u = new URL(a.href, location.href), t = (a.textContent || a.getAttribute('aria-label') || '').trim(),
      to = u.pathname + (u.hash.startsWith('#/') || u.hash.startsWith('#!/') ? u.hash.split('?')[0] : '');
    if (u.host === location.host && !/log[ _-]?(out|off)|sign[ _-]?(out|off)|end[ _-]?session|delete|remove/i.test(to + ' ' + t) && !seen.has(to)) { seen.add(to); out.links.push([to, t.slice(0, 40)]); } } catch (e) {} }
  for (const el of document.querySelectorAll('[role=tab], [aria-haspopup], [aria-expanded], [role=menuitem], details > summary'))
    out.states.push([el.getAttribute('role') || el.tagName.toLowerCase(), (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 40)]);
  return JSON.stringify({links: out.links.slice(0, 40), states: out.states.slice(0, 40)}); })()"
```

## Look for broken output (one call, read-only)
Step 5 runs this once after the page loads and after each submit or big state change (explore and ticket mode alike). One `--raw eval` lists visible breakage the page doesn't report: `NaN`, `undefined`, `[object Object]` or `Invalid Date` in visible text (a fixed toast, modal or banner included: visibility comes from the element's box and style, not `offsetParent`, which is `null` for every `position: fixed` element), leaked template or i18n text (`{{code}}`, `${price}`, `{0}`, `errors.settings.save_failed`), a count that says `0 … shown` (check the request behind it), a value shown as the machine keeps it (a raw timestamp `2026-11-15T14:30:00Z`, an internal name `IN_PROGRESS`; the first two of each), a percentage over 100 (a part larger than its total: look for a filter or another page changing the total), broken images, a page wider than the window, a region still busy, controls with no accessible name, duplicate `id`s, and a visible button or field another element covers (a sticky bar or an overlay: `elementFromPoint` at its centre hits something else). Code blocks and text areas are skipped. Each hit is a lead, never a bug by itself: check it like any other result (oracle, 2/2 repro).
```
playwright-cli -s=qa --raw eval "(() => { const out = [], vis = el => { if (!el || !el.isConnected) return false; const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; };
  const bad = /\b(NaN|undefined|Invalid Date)\b|\[object Object\]/, tpl = /\{\{[^}]{1,60}\}\}|\x24\{[^}]{1,60}\}|\{\d\}/,
    key = /\b(?:errors?|labels?|common|messages?|validation|buttons?|tooltips?|placeholders?)(?:\.[a-z][a-z0-9_-]*)+\b/, zero = /\b0 \w+ (?:shown|found|loaded)\b/i,
    iso = /\b\d{4}-\d\d-\d\dT\d\d:\d\d/, name = /\b[A-Z]{2,}(?:_[A-Z]{2,})+\b/, pct = /(\d{3,})(?:[.,]\d+)?\s?%/, seen = {};
  const raw = (kind, t) => { seen[kind] = (seen[kind] || 0) + 1; if (seen[kind] < 3) out.push(kind + ': ' + t.slice(0, 60)); };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) { const t = n.textContent.trim(), p = n.parentElement; if (!t || !p || /^(SCRIPT|STYLE|TEXTAREA|PRE|CODE|NOSCRIPT)$/.test(p.tagName) || !vis(p)) continue;
    if (bad.test(t)) out.push('text: ' + t.slice(0, 80)); if (tpl.test(t) || key.test(t)) out.push('leaked template or key: ' + t.slice(0, 80)); if (zero.test(t)) out.push('zero count (see the request behind it): ' + t.slice(0, 60));
    if (iso.test(t)) raw('raw timestamp', t); if (name.test(t)) raw('internal name', t); const m = t.match(pct); if (m && +m[1] > 100) raw('over 100%', t); }
  for (const i of document.images) if (i.complete && !i.naturalWidth && vis(i)) out.push('broken image: ' + (i.alt || i.src).slice(0, 80));
  if (document.documentElement.scrollWidth > innerWidth + 2) out.push('wider than the window');
  for (const b of document.querySelectorAll('[aria-busy=true]')) if (vis(b)) out.push('still busy: ' + (b.id || b.tagName));
  const ids = {}; for (const e of document.querySelectorAll('[id]')) if (!e.closest('svg')) ids[e.id] = (ids[e.id] || 0) + 1;
  for (const k in ids) if (ids[k] > 1) out.push('duplicate id: ' + k + ' x' + ids[k]);
  for (const e of document.querySelectorAll('button, a[href], input:not([type=hidden]), select, textarea')) { if (!vis(e)) continue;
    if (!(e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || e.textContent.trim() || (e.labels && e.labels.length) || e.title || e.placeholder || (/^(submit|button|reset)$/.test(e.type) && e.value))) out.push('no accessible name: ' + e.outerHTML.slice(0, 60));
    const r = e.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (e.matches('button, [type=submit], input:not([type=hidden]), select, textarea') && x > 0 && y > 0 && x < innerWidth && y < innerHeight) { const t = document.elementFromPoint(x, y);
      if (t && t !== e && !e.contains(t) && !t.contains(e) && !(t.closest('label') && t.closest('label').control === e)) out.push('covered: ' + (e.id || e.name || e.textContent.trim().slice(0, 30) || e.tagName) + ' by ' + (t.id || t.tagName)); } }
  return out.slice(0, 20).join(' | ') || 'none'; })()"
```

## Browser conditions: narrow window, dark mode, unsaved edits, uploads
Every hit is a lead: give it an oracle and repro it twice.

**Narrow window** (once per page, after the given cases). Note the window size, then in one Bash call resize, run the broken-output probe above, and restore the size even when the probe fails (`;`, not `&&`):
```
playwright-cli -s=qa --raw eval "innerWidth + ' ' + innerHeight"
playwright-cli -s=qa resize 390 844; playwright-cli -s=qa --raw eval "<the broken-output probe's script>"; playwright-cli -s=qa resize <the width> <the height>
```
A page wider than the window, or a button or field another element covers, at phone width is a QUESTION (does this app support a window that narrow?) unless an AC, the card or `RULES:` says it does: then it is a bug (`ui-state`).

**Dark mode** (only when the brief's `FOCUS:` names dark mode, theme, colour scheme or contrast). Run this read-only eval in light, then dark, then clear the emulation:
```
playwright-cli -s=qa --raw eval "(() => { const num = c => /^rgba?\(/.test(c) ? c.match(/[\d.]+/g).slice(0, 3).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }) : null,
    lum = c => { const x = num(c); return x ? 0.2126 * x[0] + 0.7152 * x[1] + 0.0722 * x[2] : NaN; };
  const bgOf = el => { for (; el; el = el.parentElement) { const c = getComputedStyle(el).backgroundColor, a = c.match(/[\d.]+/g); if (a && (a.length < 4 || +a[3] > 0.5)) return c; } return 'rgb(255, 255, 255)'; };
  const out = [], w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode()) && out.length < 8;) { const p = n.parentElement, t = n.textContent.trim(); if (!t || !p || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(p.tagName)) continue;
    const r = p.getBoundingClientRect(), cs = getComputedStyle(p); if (!(r.width > 0 && r.height > 0) || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const a = lum(cs.color) + 0.05, b = lum(bgOf(p)) + 0.05, ratio = Math.max(a, b) / Math.min(a, b); if (ratio < 1.5) out.push(t.slice(0, 30) + ' (' + ratio.toFixed(1) + ')'); }
  return 'page background ' + bgOf(document.body) + ' | text you can barely read: ' + (out.join(', ') || 'none'); })()"
playwright-cli -s=qa set-color-scheme dark; playwright-cli -s=qa --raw eval "<the same script>"; playwright-cli -s=qa clear-color-scheme
```
The page background is the same in both: the app ignores dark mode, which is not a bug (never ask for it). The background changed and text appears in the dark run that the light run didn't list (light text on a light panel, dark text on dark): a hybrid rendering bug (`ui-state`), with a screenshot in each scheme as evidence.

**Unsaved edits** (a page with an edit or create form). `fill` one field with a `qa-sdet-` value, then `click` a link that leaves the page (a menu entry or Cancel; never a delete link). The page then shows one of three things:
- A dialog: the output has `### Modal state` and `"confirm" dialog` (or `beforeunload`). Run `playwright-cli -s=qa dialog-dismiss` (it stays on the page) and read the field: it must still hold the value. Never `dialog-accept` here: it leaves the page and loses the edit, and a confirm you didn't expect may be a destructive one.
- A modal inside the page (`Discard changes?`): press its Cancel or Stay button, never Discard.
- No guard: the page is left and the edit is lost. Policy-dependent: a QUESTION unless an AC says unsaved changes are protected.

**Uploads** (a page with `<input type="file">`; uploads are the page's own feature, so skip it on a page where one could reach other users: `RULES:`). Click the input (or the button that opens it): the output shows `[File chooser]: can be handled by upload`. Then give it a fixture from the plugin's `<refs>/upload-fixtures/` (tiny, synthetic, no secrets), one per check, and read the page's answer:
```
playwright-cli -s=qa upload '<refs>/upload-fixtures/qa-sdet-empty.txt'
playwright-cli -s=qa upload '<refs>/upload-fixtures/qa-sdet-fake.pdf'
playwright-cli -s=qa upload '<refs>/upload-fixtures/qa-sdet-1x1.png'
```
- `qa-sdet-empty.txt` (0 bytes): refused with a clear message, unless an AC accepts empty files.
- `qa-sdet-fake.pdf` (plain text named `.pdf`): a page that promises PDFs must not take it on its name; the server answers a 4xx, not a saved "document" (check `requests`).
- `qa-sdet-1x1.png` (a valid 1x1 image): accepted or refused for a stated limit (minimum size, dimensions); the preview must not break.
Each upload that is saved creates a record: name it `qa-sdet-…` where the page lets you, and delete it in step 9 like any record you made. Never upload anything but these three files.

## Second tab or popup
A click that opens another tab (a report, a preview, a detached view): the tab opening is not the result. List the tabs, switch to the new one and read it; then close it and go back to tab 0, since every later command acts on the selected tab:
```
playwright-cli -s=qa tab-list; playwright-cli -s=qa tab-select 1; playwright-cli -s=qa --raw eval "document.title + ' | ' + location.href + ' | ' + document.body.innerText.slice(0, 200)"
playwright-cli -s=qa tab-close 1; playwright-cli -s=qa tab-select 0
```
Oracle: the new tab shows what was asked for, or it closed and the first tab says why. A tab still `about:blank` with nothing in it after the call behind it ended (`requests` in tab 0), and no message in the first tab, is a bug. Take the evidence image in the tab that shows the problem.

## Type key by key
`fill` sets a whole value at once and the batch eval sets it without a keystroke: both skip what typing does to a field made of parts (a time or date with its own hour and minute boxes, a masked number). Type it as a user does, then read what the field shows:
```
playwright-cli -s=qa click "<locator of the first part>"; playwright-cli -s=qa press Control+a; playwright-cli -s=qa press Backspace; playwright-cli -s=qa type '09'; playwright-cli -s=qa press Tab; playwright-cli -s=qa type '30'; playwright-cli -s=qa --raw eval "[...document.querySelectorAll('<css of the parts>')].map(e => e.value).join(':')"
```
Oracle: the field shows the time typed (`09:30`). A part that empties or resets while you type the next is a bug (`keyboard-a11y`), even when `fill` gives the right value.

## Settled, not just loaded
A spinner or overlay going away means the request ended, not that the page finished drawing, and a count that streams in can change after you read it. Before judging a list against its count, read the value until two reads 300 ms apart agree:
```
playwright-cli -s=qa --raw eval "(async () => { const read = () => document.querySelector('<css of the count>').textContent.trim(); let a = read(); for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 300)); const b = read(); if (b === a) return a; a = b; } return a + ' (still changing)'; })()"
```
Compare values read at the same moment (the filter's own count and the list's total in one eval). A map is ready when its canvas is visible and the feature call has answered, not when its buttons show.

## A control that moved
A toolbar control the card names that `find` doesn't show: before calling it missing, open the toolbar's overflow menu (`More`, `…`) and look there; toolbars move controls in and out with the window's width and with counts. Keep the card's locator (role and name); a snapshot ref is never saved. Dialogs stack: act in the top one by naming it, `getByRole('dialog', { name: 'Delete note' }).getByRole('button', { name: 'Delete' })` (a bare `Delete` matches every row's button too), and close them one at a time: Escape must close only the top layer and leave the form under it as it was.

## Server-boundary probes (no secrets read)
A limit the page enforces is not proof the server does: `max`, `required`, a `<select>` list and a disabled button stop a value before it is sent. Test the server in this order; `curl` carries no sign-in and the guard denies a write sent with `eval`.

**1. Bypass the page's own checks, then submit through the UI** (every run, no flag). The app's own signed-in client sends the request, whatever its auth scheme. One eval relaxes the form, sets one value (a number past `max`, a decimal, `   `, text past `maxlength`, a value outside a `<select>`'s list) and submits it; use `qa-sdet-` text for a text field. The record it creates, if the server accepts it, is deleted in step 9:
```
playwright-cli -s=qa --raw eval "(() => { const f = document.querySelector('form'), el = f.querySelector('[name=speed]'), v = '131';
  f.noValidate = true;
  for (const e of f.elements) for (const a of ['max', 'min', 'maxlength', 'minlength', 'required', 'pattern', 'step', 'readonly']) e.removeAttribute(a);
  if (el.tagName === 'SELECT' && ![...el.options].some(o => o.value === v)) el.add(new Option(v, v));  // an option the list doesn't offer
  const proto = {INPUT: HTMLInputElement, TEXTAREA: HTMLTextAreaElement, SELECT: HTMLSelectElement}[el.tagName].prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);  // the native setter: React inputs too
  el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
  f.requestSubmit();
  return 'sent ' + el.name + '=' + v; })()"
playwright-cli -s=qa requests --filter '/api/'
playwright-cli -s=qa response-body <#>
```
- The value the UI refuses must be refused by the server too: a 4xx with an error and no record. A 2xx, a saved record (reload and read it), or a 500 is a bug (boundary / invalid-format / empty-null). Say in the draft that the page's check was bypassed, with the request body and status (`BUG-<n>-network.txt`).
- If the page puts the attribute back on re-render, this one call is still enough: it sets and submits in the same call. A form with no `<form>` element (a button that calls the API): relax the inputs, then `click` the real button.

**2. Read with `qa-mem api-probe`** (every run). One request from the signed-in browser: it prints `{status, ok, headers, body}`, with contract headers only and a scrubbed body cut at 4000 characters, and counts as one browser command:
```
qa-mem api-probe GET '<qaBaseUrl>/api/zones/<id>'
qa-mem api-probe OPTIONS '<qaBaseUrl>/api/zones'
qa-mem api-probe GET '<qaBaseUrl>/api/admin/stats'
```
- Check what the UI hides: the status, `content-type` (JSON, not an HTML error page), `cache-control` and `etag` on data, `location` after a create, and `allow`. An endpoint the card lists for another page or for no page (`page *`) that answers 200 with data to this role while the AC says it is restricted is a `permissions` bug; with no AC it is a QUESTION.
- It refuses login, session and token paths and credential headers, and a URL outside `allowedHosts`. A 401 or 403 on an app that attaches its token in JavaScript only means the probe has no token: add a `not_tested` line, category `needs_setup`, and use the UI route (1) instead. Never a bug.

**3. Writes with `qa-mem api-probe`, only when the brief says `api_writes: yes`** (otherwise the guard refuses them). One mass-assignment probe per write endpoint, on a record you created this run: add one field the form never sends (the read-only fields a GET of the record shows: `approved`, `status`, `role`, `createdBy`, `id`), then read the record back. The tool allows a POST only with a `qa-sdet-` value in the body, and a PUT, PATCH or DELETE only on a record that shows `qa-sdet-` (it reads the record first, so each costs 2 browser commands). A bulk or list URL is refused:
```
qa-mem api-probe POST '<qaBaseUrl>/api/zones' --json '{"name": "qa-sdet-zone-1", "speedLimit": 50, "approved": true}'
qa-mem api-probe GET '<qaBaseUrl>/api/zones/<id>'
qa-mem api-probe DELETE '<qaBaseUrl>/api/zones/<id>'
```
- A stored extra field (`approved` true although the form can't set it) is a bug (permissions); a malformed body or a wrong `--content-type text/plain` that gives 500 instead of 400 or 415 is another. Delete what you created in step 9 the same way (`DELETE`), or list it as `Left in the app:`.

**HTML-encoding canary** (harmless markup, entered through the UI like any value): type `qa-sdet-canary <b>b</b> & &amp; "q"` in a text field, save, then read it back where it is shown (the list, the detail page, a toast):
```
playwright-cli -s=qa --raw eval "JSON.stringify([...document.querySelectorAll('body *')].filter(e => e.textContent.includes('qa-sdet-canary') && ![...e.children].some(c => c.textContent.includes('qa-sdet-canary'))).slice(0, 5).map(e => ({tag: e.tagName, text: e.textContent.slice(0, 80), injected: e.querySelectorAll('b').length})))"
```
It must show the text as typed (`<b>b</b> & &amp; "q"`) and `injected` 0. A `<b>` element built from it (`injected` above 0, bold text) is unescaped output; `&amp;amp;` or `&lt;b&gt;` shown as text is double-encoding. Never a `<script>` tag or an event handler.

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
