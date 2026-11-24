Fake source for the fixture app in `tests/fixture/` (served as a static site), used to test `/qa-sdet:qa-init`.
Planted on purpose: a code rule the page breaks (speed max 130), a test id the page lacks (`speed-zone-save`),
a backend endpoint no page calls (`/api/admin/stats`), and files the scan must skip (`.env`, `secrets.json`, `node_modules`, `dist`).
