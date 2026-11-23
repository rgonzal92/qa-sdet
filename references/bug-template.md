# <Short title: what breaks, where>

**Classification:** CONFIRMED | SUSPECTED · **Severity:** HIGH · **Route:** /path · **Build:** <build> · **Browser:** <config browser> headless (or the MCP's browser)
**Ticket:** <KEY or none> · **Repro:** 2/2 · **Found:** <date> · **qa-mem hash:** <output of `qa-mem hash <route> '<symptom>'`>

## Steps
1. …
2. …

## Expected
<what should happen> — **Oracle:** <AC-x | rule | invariant>

## Actual
<what happened, exact text/values>

## Evidence
- Screenshot: ../evidence/<file>.png
- Network: `<METHOD> <path>` → <status> (redacted)
- Console: <message>
- Repro script: ../repro/<id>.spec.ts

## Notes
<possible cause, clearly a hypothesis; related pages>

<!-- To file (user only): acli jira workitem create --project <PROJ> --type Bug --summary "<title>" --description-file <absolute path of this file> -->
