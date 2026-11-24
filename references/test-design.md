# Test design techniques (pick per requirement)

- **Equivalence partitioning (EP):** one case per valid class + one per invalid class.
- **Boundary value analysis (BVA):** for range [a,b] test a-1, a, a+1, b-1, b, b+1.
- **Decision table:** list conditions (role, state, flag) × outcomes; one case per meaningful column; collapse "don't care".
- **State transition:** draw states and events; cover every valid transition once + key invalid ones (event in wrong state).
- **Pairwise:** for 3+ independent params, cover every pair of values, not the full product.
- **Negative / error guessing:** missing auth, stale data, double submit, network error (caused only with `--faults`), past known bugs (`qa-mem bugs`).

Rules
- Every case's expected result traces to an AC, a stated requirement (a card fact marked `from X`), or an invariant; code rules give inputs, not expected results. If none, it is a question, not a case.
- Test at the cheapest level that proves it; flag cases that belong in API/unit tests.
- Priority: P1 = AC core/money/safety/data loss, P2 = common path variants, P3 = edge/cosmetic.
