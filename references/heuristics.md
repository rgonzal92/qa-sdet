# Exploration heuristics

IDs must match `HEURISTICS` in `bin/qa-mem`. Record each one tried in the run JSON's `coverage` (`run-json.md`).

| ID | Try | Typical oracle |
|---|---|---|
| happy-path | Ordinary journey end to end, once, to learn the baseline | Feature does what its labels/AC say |
| boundary | min, max, just inside/outside, 0, negative, huge | Field limits, AC, server validation matches client |
| empty-null | Blank required fields, whitespace only, cleared selects | Clear error, nothing saved |
| invalid-format | Wrong types, bad dates, letters in numbers, bad emails/IDs | Rejected with useful message |
| unicode-whitespace | Accents, emoji, RTL, leading/trailing spaces, very long strings | Stored and shown intact; no layout break |
| duplicate | Same record twice, double-click submit, same name (on your own `qa-sdet-` records) | No duplicate rows / one request |
| sequence-repeat | Repeat, reorder, skip steps, rapid edits | State stays consistent |
| back-refresh | Back/forward/refresh mid-flow, deep link, new tab | No lost data, no resubmit, correct state |
| sort-filter-paginate | Sort every column, combine filters, last page, page size, filter + paginate | Order/filter correct across pages; count matches |
| error-handling | Trigger server errors (needs --faults for mocked ones) | UI shows failure, not success |
| loading-empty-states | No data, slow data, one row, many rows | Sensible empty/loading UI |
| keyboard-a11y | Tab order, Enter/Escape, focus after dialog, labels | Fully operable by keyboard; named controls |
| console-errors | `console error` after each step | No uncaught errors |
| network-failures | `requests` after each step: 4xx/5xx, duplicate calls | A 4xx answering input you sent on purpose (invalid value, forbidden action) is fine; a failed call the page needs for its own data must show as an error, never as a result ("0 found", "saved") |
| permissions | Hidden/disabled actions, direct URLs to restricted pages | Server enforces, not only UI |

## Traffic-domain ideas (record under the nearest heuristic ID above)
- Time/zone: timestamps near midnight, DST, local vs UTC display of incidents/closures.
- Geo/IDs: lat/long bounds, segment/intersection IDs that don't exist, duplicate device IDs.
- Status transitions: active → cleared → reopened incidents; schedules that overlap.
- Map page: features shown match the table/API data; filters/layers change requests; empty bbox.
