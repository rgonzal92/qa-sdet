# Classification and severity

Classification (exactly one):
- **CONFIRMED**: reproduced ≥2 times, violates AC / stated rule / clear invariant (data loss, wrong value saved, crash, uncaught error, success shown on failure).
- **SUSPECTED**: seen once or not reliably reproducible, or oracle is weak.
- **QUESTION**: behavior depends on product policy nobody stated: a question for the product owner, not a bug.
- **ENV_FAILURE**: the browser, the login session or a tool failed, or test data you need is missing and you can't create it: not the product. An app request that fails while the page works is product behaviour, not ENV_FAILURE: a failed call shown as a result (a count, an empty list, "saved") is a bug.

Severity (impact, not looks):
- **CRITICAL**: security hole, data loss/corruption, core function down.
- **HIGH**: main workflow blocked, wrong data saved/shown, server allows a forbidden action.
- **MEDIUM**: function broken with workaround, or limited scope.
- **LOW**: minor, cosmetic with no data impact.
