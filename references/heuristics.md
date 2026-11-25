# Exploration heuristics

IDs match `HEURISTICS` in `lib/qa_sdet/core.py`. Record each one you try in the run JSON `coverage`.

| ID | Try | Typical oracle |
|---|---|---|
| happy-path | The ordinary journey once, for a baseline | Does what its labels and AC say |
| boundary | min, max, just in or out, 0, negative, huge; then past the page's own limit, by bypassing its checks and submitting through the UI (tips: Server-boundary probes); markup characters (`<b>`, `&`, quotes) read back | Field limits, AC; the server refuses what the UI refuses; text shown as typed, not rendered or double-encoded |
| empty-null | Blank required fields, only spaces, cleared selects | Clear error, nothing saved |
| invalid-format | Wrong types, bad dates, letters in numbers, bad emails or IDs | Rejected with a useful message |
| unicode-whitespace | Accents, emoji, RTL, leading or trailing spaces, very long text | Stored and shown intact; layout holds |
| duplicate | Same record twice, double-click submit (on your own `qa-sdet-` records) | One row, one request |
| sequence-repeat | Repeat, reorder or skip steps, rapid edits | State stays consistent |
| back-refresh | Back, forward, refresh mid-flow, deep link, new tab | No lost data, no resubmit |
| sort-filter-paginate | Sort every column, combine filters, last page, page size | Order and filter right across pages; count matches |
| error-handling | Trigger server errors (mocked ones need `--faults`) | The UI shows failure, not success |
| loading-empty-states | No data, slow data, one row, many rows | Sensible empty and loading UI |
| keyboard-a11y | Tab order, Enter and Escape, focus after a dialog, labels | Fully usable by keyboard; named controls |
| console-errors | `console error` after each step | No uncaught errors |
| network-failures | `requests` after each step: 4xx/5xx, duplicate calls | A 4xx answering input you sent on purpose is fine; a failed call the page needs must show as an error |
| permissions | Hidden or disabled actions, direct URLs to restricted pages; a restricted endpoint read with `qa-mem api-probe GET`; with `api_writes: yes`, an extra field (`approved`, `role`, `status`) sent on your own `qa-sdet-` record | The server enforces, not only the UI; no field the form can't set is stored |

Domain ideas (record under the nearest ID): time zones, DST and near-midnight timestamps; lat/long bounds, IDs that don't exist, duplicate device IDs; status changes (active, cleared, reopened) and overlapping schedules; on a map page, features match the table and API data, and filters change the requests.
