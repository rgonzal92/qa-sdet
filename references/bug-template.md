Rules for a draft a developer can act on from Jira (the user files it with `--description-file`, so the whole file becomes the Jira description):
- Title: what breaks, where; no `'` character (the filing command puts it in single quotes).
- Env and Steps: step 1 opens the full URL (`qaBaseUrl` + the concrete path), never a bare `/path`.
- Browser: the config `browser` value (chrome, msedge, firefox or webkit), or with `browser=mcp:…` the words `Playwright MCP browser`.
- Impact: who is hurt and how, then the severity.md line that gives the severity.
- No secrets or personal data anywhere in the draft or the spec: no passwords, tokens, cookies, auth headers, session or API keys, and no names, emails, phone numbers or licence plates of real people seen on the page (write "a user's email", or use a `qa-sdet-` record you created).
- No filing command in the draft: the skill prints it for the user.

Write this shape (everything below the line):

---
# <Short title: what breaks, where>

**Classification:** CONFIRMED | SUSPECTED · **Severity:** HIGH · **Env:** <qaBaseUrl> · **Route:** /path · **Build:** <build> · **Browser:** <browser>
**Ticket:** <KEY or none> · **Repro:** 2/2 · **Found:** <date> · **qa-mem hash:** <output of `qa-mem hash '<route>' '<symptom>'`>

## Steps
1. Open <qaBaseUrl>/<concrete path>
2. …

## Expected
<what should happen> — **Oracle:** <AC-x | rule | invariant>

## Actual
<what happened, exact text/values>

## Impact
<who is affected and how>. <SEVERITY>: <the severity.md line it matches>.

## Evidence (attach these files to the Jira item: the local links below don't open from Jira)
- Screenshot: BUG-<n>.png ![BUG-<n>](../evidence/BUG-<n>.png) (red box marks the problem; without the box, say where on the page it is)
- Network: `<METHOD> <path>` → <status> (redacted)
- Console: <message>
- Repro spec: BUG-<n>.spec.ts (../repro/BUG-<n>.spec.ts); run it with `BASE_URL=<qaBaseUrl> npx playwright test BUG-<n>.spec.ts`

## Notes
<possible cause, clearly a hypothesis; related pages>
