# A bug: severity, draft, repro spec, hash

Severity (impact, not looks): **CRITICAL** security hole, data loss or corruption, core function down. **HIGH** main workflow blocked, wrong data saved or shown, the server allows a forbidden action. **MEDIUM** broken with a workaround, or limited scope. **LOW** minor, no data impact.

Per CONFIRMED or SUSPECTED bug write `<out>/bugs/BUG-<n>.md`, `<out>/repro/BUG-<n>.spec.ts` and the evidence image (tips file). The user files the draft with `--description-file`, so the whole file becomes the Jira description:
- Title: what breaks and where; no `'` character (the filing command single-quotes it).
- Step 1 opens the full URL (`qaBaseUrl` + the concrete path), never a bare `/path`. Browser = the config `browser`.
- No secrets or personal data in the draft or the spec: no passwords, tokens, cookies, auth headers, keys; no names, emails, phone numbers or plates of real people (write "a user's email", or use a `qa-sdet-` record you made).
- No filing command in the draft: the skill prints it for the user.
- **Hash:** `qa-mem hash '<route>' '<symptom>'`; `<route>` is the page where the bug shows (the bug's `route` in the run JSON) and the symptom is the one you put there too. Single quotes; a `'` in the symptom is written `'\''`.

```
# <Short title: what breaks, where>

**Classification:** CONFIRMED | SUSPECTED · **Severity:** HIGH · **Env:** <qaBaseUrl> · **Route:** /path · **Build:** <build> · **Browser:** <browser>
**Ticket:** <KEY or none> · **Repro:** 2/2 · **Found:** <date> · **qa-mem hash:** <hash>

## Steps
1. Open <qaBaseUrl>/<concrete path>
2. …

## Expected
<what should happen> — **Oracle:** <AC-x | rule | invariant>

## Actual
<what happened, exact text and values>

## Impact
<who is hurt and how>. <SEVERITY>: <the severity line above that matches>.

## Evidence (attach these files to the Jira item: the local links below don't open from Jira)
- Screenshot: BUG-<n>.png ![BUG-<n>](../evidence/BUG-<n>.png) (red box marks the problem)
- Video: BUG-<n>.webm (../evidence/BUG-<n>.webm), the clean-state repro (SUSPECTED: the attempt that didn't show it)
- Network: `<METHOD> <path>` → <status> (redacted)
- Console: <message>
- Repro spec: BUG-<n>.spec.ts (../repro/BUG-<n>.spec.ts); run it with `BASE_URL=<qaBaseUrl> STORAGE_STATE=<a signed-in storage state file> npx playwright test BUG-<n>.spec.ts` (STORAGE_STATE only when the app needs a sign-in)
- API repro (only for a bug in an API call: a 4xx/5xx, or the server accepting what it must refuse): `curl -s -X <METHOD> '<qaBaseUrl><path>' -H 'Content-Type: application/json' -H 'Authorization: <TOKEN>' -d '<the request body, secrets and personal data replaced>'`, with `<TOKEN>` as written, never a real value

## Notes
<possible cause, clearly a hypothesis; related pages>
```

Repro spec: the recorded code with locators from the card or `generate-locator` (role, label, test id). First lines:
```ts
import { test, expect } from '@playwright/test';
test.use({ baseURL: process.env.BASE_URL ?? '<qaBaseUrl>',
  ...(process.env.STORAGE_STATE ? { storageState: process.env.STORAGE_STATE } : {}) });
```
Use a relative `page.goto('<path>')` with the concrete path you visited (`/incidents/4521`, not `/incidents/:id`), and wait for elements, not `waitForTimeout`. A bug on a `qa-sdet-` record you created (step 9 deletes it): start on its create or list page and create a new `qa-sdet-` record in the spec, never `goto` the deleted record's path. End with an assertion (`expect(`) of the **expected** behaviour, so the spec fails while the bug exists. `qa-mem audit-bugs` checks drafts and specs against this file.
