# Running the explorer: setup, delegate, teardown, reply

`<plugin>` is the plugin root, `<project>` the project folder; outputs go under `<project>/qa-out/`. Variables don't survive between Bash calls: write full paths.
**Quoting:** paths (they can hold spaces) and text from the user, a ticket or a page go into a shell command only inside single quotes, each `'` in it written `'\''` (`can't` becomes `'can'\''t'`), never double-quoted or bare. The one exception: a card locator holds `'` itself, so it goes in double quotes when it has no `$`, backtick or `"`.

## Config
Read `~/.config/qa-sdet/config.json`. Missing: ask once for app, qaBaseUrl, allowedHosts (a list: the qaBaseUrl host and every API host), ssoHost (a host, not a URL), jiraProject, jiraKeyPrefix (e.g. `PROJ`), mapRoute, buildSource (`{"type":"selector|url|header","value":"…"}`); save it.
`browser` missing or not `chrome`, `msedge`, `firefox` or `webkit`: run `qa-mem browsers`, ask which installed one to use (show the install command for the others), save it. Never pick it for the user.
After any config write run `qa-mem doctor` (no other time here, except when a tool fails) and fix its `FIX` lines about the config: the text after `->` is the fix; save it only on the user's yes, then run doctor again. A declined fix stops the run: say which line is open.

## Run folder and slug
`qa-mem rundir '<project>' --keys K1,K2` (qa-ticket), `--route '<route>'` (qa-explore) or `--label '<text>'` prints `<run>`. Slug = the route without its leading `/` and extension, other `/` as `-`, numeric or UUID segments as `id`, no `#`, `!` or `?…` (`/form.html` is `form`, `/incidents/4521` is `incidents-id`, `/#/zones` is `zones`, `/` is `home`). Routes of one run that give the same slug (`/list?status=open` and `/list?status=closed`, `/form` and `/form.html`) would share one run file, the later explorer overwriting the earlier one's results: add `-2`, `-3`, … to the later routes' slugs.

## Setup: one Bash call per explorer
```
qa-mem rundir '<project>' --route '<route>'; qa-mem marker start '<project>' [--budget N] [--minutes M] [--faults]; qa-mem card '<route>'
```
It prints `<run>`, the marker and the page card (keep the card verbatim). qa-ticket made its run folder earlier and leaves `rundir` out; a crawl has no one page and leaves `card` out. Pass only the flags the user gave; without `--budget` or `--minutes` there is no limit. Both apply per explorer (a crawl is one). `--minutes` is at most 480: pass 480 and say so. Only `--faults` lets the explorer use `route` and `network-state-set`.

## Delegate
Agent tool, subagent type `qa-sdet:qa-explorer` (or `qa-explorer`). The prompt holds, as literal text: target URL, `out=<run>`, `slug=<route slug>`, `refs=<plugin>/references`, mode, budget and minutes (numbers or `unlimited`), faults yes or no, the card, the given cases (id, ticket, AC, steps, expected), and in ticket mode every AC of their tickets and the known Jira bugs. If `<project>/qa-rules.md` exists (the user's safety rules), Read it once per skill run and add it under `RULES:`. A route with `:id` needs a real record id: ask the user, or give the list page's route and tell the explorer to open one record.
One explorer at a time (they share the `qa` browser session). No summary back: take `status` and `notes` from `<run>/.data/run-<slug>.json`; no file: BLOCKED, `explorer ended without a run file`. A BLOCKED explorer's summary names the fix (`/qa-sdet:qa-login`, install or choose a browser): relay it, don't retry.

## Teardown: one Bash call after each explorer, errors too
```
playwright-cli -s=qa close; qa-mem marker stop '<project>'; qa-mem report '<run>'; qa-mem prune '<project>' '<run>'
```
If `close` hangs, run `playwright-cli kill-all` (it ends only Playwright's browsers), then the rest. `report` renders `<run>/REPORT.md` (keep it as written) and `<project>/qa-out/README.md`; `prune` drops this run's page snapshots and map screenshots, and snapshots older than 7 days.

## Reply (at most 12 lines)
In this order: the explorer's status (COMPLETED, INCOMPLETE, BLOCKED); bugs as `[SEV] title`; top untested risks; the `<run>/REPORT.md` path (a `Problems reading this folder` section means some results are missing); then one next step: a BLOCKED explorer's fix, else the filing command for each CONFIRMED draft in `<run>/bugs/`, else a re-run with `--faults` when this run had none.
**Filing command** (print it, never run it: Jira writes are the user's): `acli jira workitem create --project <PROJ> --type Bug --summary '<the draft's # title>' --description-file '<absolute draft path>'`, `<PROJ>` = the ticket's project key, else `jiraProject`. Single quotes, never double (a title quoting page text must not run `$(…)` or backticks in their shell); drop any `'` from the title. Tell them to attach `<run>/evidence/BUG-<n>.png` (look first: screenshots aren't scrubbed) and `<run>/repro/BUG-<n>.spec.ts` (the draft's local links don't open from Jira), and to run `qa-mem bug-status <hash> FILED` after filing.
