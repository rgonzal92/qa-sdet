# Classification and severity

Classification (exactly one):
- **CONFIRMED**: reproduced ≥2 times, violates AC / stated rule / clear invariant (data loss, wrong value saved, crash, uncaught error, success shown on failure).
- **SUSPECTED**: seen once or not reliably reproducible, or oracle is weak.
- **QUESTION**: behavior depends on product policy nobody stated: a question for the product owner, not a bug.
- **ENV_FAILURE**: QA env, data, session or tool problem, not the product.

Severity (impact, not looks):
- **CRITICAL**: security hole, data loss/corruption, core function down.
- **HIGH**: main workflow blocked, wrong data saved/shown, server allows a forbidden action.
- **MEDIUM**: function broken with workaround, or limited scope.
- **LOW**: minor, cosmetic with no data impact.
