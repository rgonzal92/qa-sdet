---
name: qa-explore
description: Exploratory bug hunt on one or more pages of the QA web app (or changed/top pages from memory gaps), no ticket needed, with annotated bug images, repro specs and a readable report. Use when asked to explore or find bugs on a page, URL, changed pages or feature, or what to explore next.
argument-hint: <url | route | page name>... | --changed | --top N [--faults] [--budget N] [--minutes M] [--focus '<charter>'] [--verify]
---

Explore and find bugs: $ARGUMENTS

Plugin root: `${CLAUDE_SKILL_DIR}/../..`. Project: `${CLAUDE_PROJECT_DIR}`. First read `${CLAUDE_SKILL_DIR}/../../references/run-setup.md`. `--faults`, `--budget N` and `--minutes M` are optional and apply per page (they go to `qa-mem dispatch`); without them there is no fault injection and no limit. `--focus '<charter>'` (optional, e.g. `--focus 'date filters across time zones'`) narrows the session to that charter: it goes to dispatch too, which puts it in the brief as `FOCUS: <charter>`.

1. **Config and target(s):**
   - No target and neither `--changed` nor `--top`: run `qa-mem gaps`, show its header and first 5 page lines, and ask which page(s) to explore (its `Next:` line is the top pick); go on with the answer.
   - `--changed`: run `qa-mem gaps --changed --top <N or 5> --json` (default cap 5 unless `--top N` is also given). If `pages` is empty, say no mapped pages changed since their last visit and stop.
   - `--top N` (without `--changed`): run `qa-mem gaps --top N --json`. If `pages` is empty, show `qa-mem gaps` and stop.
   - With `--changed` or `--top`, `pages` holds only routes an explorer can open: a detail route (`/incidents/:id`) with an `entry_route` is dispatched as `--route '<entry_route>' --open-record` (the explorer opens a record there); `needs_id` lists detail routes nobody reached yet: don't explore them, name them in the reply.
   - Explicit target(s) (one or more URLs, routes or page names): a full URL as given (every URL's host must be in `allowedHosts`; if any is not, stop before opening the browser); a route is `qaBaseUrl + route`; a page name resolves via `qa-mem card '<name>'` (`qa-mem code` lists mapped pages; NO CARD: ask the user for the route and pass on the card's `tip:` line).
2. **Pre-flight session check** (run-setup): verify the QA session is active (recovering inline with headed `qa-login` if expired) before creating a run folder.
3. **Run folder and plan:** create ONE run folder: `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --route '<route>'` for one page, `qa-mem rundir '${CLAUDE_PROJECT_DIR}' --label '<changed-Npages or explore-Npages>'` for 2+ pages. Write `<run>/.data/plan.json`: `{"kind": "explore", "title": "<route or comma-separated routes>", "created": "<date -u +%Y-%m-%dT%H:%M:%SZ>", "env": {"url": "<first target>", "browser": "<config browser>"}, "tickets": [], "cases": [], "questions": []}`; with `--focus` append `, focus: <charter>` to `title`. When the user listed checks to run, they are `cases` (fields in `${CLAUDE_SKILL_DIR}/../../references/planning.md`).
4. **Explore:** for each target page, sequentially (one explorer at a time; dispatch gives each route its own slug): `qa-mem dispatch '<run>' --route '<route>' --mode explore` (run-setup's Setup; plus `--focus '<charter>'` and the limits), delegate with its brief, teardown. If an explorer returns `BLOCKED`, skip remaining pages and keep its summary.
With `--verify`: after the last teardown, run-setup's Second opinion (a `qa-sdet:qa-reviewer` read-only check of the serious drafts; its reply saved as `<run>/.data/review.json`).
5. **Reply** (run-setup).
