# code-map-<area>.json (written by the qa-mapper agent)

`/qa-sdet:qa-init` first runs `qa-mem codemap-scan`, which writes `qa-out/init/scan-<area>[-<n>].json` (the repos, their
git commit and the exact files to read; never edit it; a large area gets several, one per mapper). Each mapper reads its
files and writes the matching `qa-out/init/code-map-<area>[-<n>].json`; the plugin's hook ingests it (table `codemap`).

```json
{
  "scan_id": "frontend-20261123-181335",
  "area": "frontend",
  "rows": [
    {"repo": "/abs/path/traffic-ui", "page": "/zones/new", "kind": "route",   "label": "Create speed zone", "value": "SpeedZoneForm", "file": "src/routes.ts:14"},
    {"repo": "/abs/path/traffic-ui", "page": "/zones/new", "kind": "locator", "label": "Speed limit",       "value": "getByLabel('Speed limit (km/h)')", "file": "src/zones/SpeedZoneForm.tsx:22"},
    {"repo": "/abs/path/traffic-ui", "page": "/zones/new", "kind": "rule",    "label": "speed",             "value": "min 0, max 130 (react-hook-form register)", "file": "src/zones/SpeedZoneForm.tsx:22"},
    {"repo": "/abs/path/traffic-ui", "page": "/zones/new", "kind": "api",     "label": "save zone",         "value": "POST /api/zones", "file": "src/api/zones.ts:8"}
  ]
}
```

A backend map (its own scan, `area` `backend`) holds only page `*` rows:

```json
{
  "scan_id": "backend-20261123-181335",
  "area": "backend",
  "rows": [
    {"repo": "/abs/path/traffic-api", "page": "*", "kind": "api",  "label": "POST /api/zones",            "value": "create zone (zones controller)", "file": "routes/zones.js:10"},
    {"repo": "/abs/path/traffic-api", "page": "*", "kind": "rule", "label": "POST /api/zones speedLimit", "value": "integer, min 0, max 130, required (Joi)", "file": "routes/zones.js:6"}
  ]
}
```

- `scan_id`, `area`: copied from the scan file. A mismatch (an older scan, or the wrong area) rejects the map.
- `repo`: the scan file's `repos[].path` the row came from. It may be omitted when only one repo in the scan has files.
- `file`: `<path relative to repo>:<line>` (`./src/x.tsx` and an absolute path inside the repo are accepted too). It must be one of that repo's `files` in the scan (a later re-scan could never replace rows from other files). If any row has a repo or file that isn't in the scan, the whole file is rejected and the hook says why: fix or remove those rows and rewrite it (rows are never silently lost).
- `page`: the app route the row belongs to, as the browser shows it (`/zones/new`, `/incidents/:id`, `/#/map`; any parameter syntax becomes `:id`), or `*` for backend rows only. Shared components' locators go on each page that renders them (`*` rows never reach a card). Use the same route string for every row of one page.
- `kind`:
  - `route`: one per page. `label` = the page's human name (title, menu text or component name), `value` = component or file.
  - `locator`: an element a tester acts on or reads. `label` = what it is ("Save button"), `value` = a Playwright locator, preferring `getByTestId('…')`, then `getByRole('…', { name: '…' })` / `getByLabel('…')`, then CSS (`[name=speed]`, `#msg`); never XPath built on layout. Copy test ids and labels exactly as written.
  - `rule`: validation or a limit as the code states it ("min 0, max 130", "required", "max 80 chars", "one of: open, closed"). Say what the code says, not what the app should do.
  - `api`: an endpoint, as `METHOD /path` (`GET /api/incidents?status=`). Frontend calls go in `value`, on the page that makes them.
- **Backend rows** go on page `*`, because the backend mapper can't see which page calls what. Write each endpoint as an `api` row with `label` = `METHOD /path`, and its request limits as `rule` rows with `label` = `METHOD /path field`, using the full path the app serves (a sub-router's mount prefix included: `PATCH /api/incidents/:id/status`, never `PATCH /:id/status`). Cards and `qa-mem code <route>` link them to every page whose frontend calls the same path (`api/incidents.json` matches `GET /api/incidents`; a call to `${API}/zones` matches `GET /api/zones` or `/api/v1/zones`, not `/api/admin/zones`). Endpoints no page calls stay findable through `qa-mem search`.
- Text is capped at 300 characters (`label` at 120). A page keeps at most 200 rows; page `*` has no cap.
- Rewriting the file is fine: it is ingested again when its content changes.
