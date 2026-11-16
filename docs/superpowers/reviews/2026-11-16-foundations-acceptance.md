# Foundation branch acceptance — 2026-11-16

All15 foundation task gates are complete. The fresh whole-branch Astra medium review found5Important and5Minor issues; the single consolidated fix wave addressed all10, and scoped re-review confirmed each. No critical approval/publication bypass was found. The branch remains unmerged/unpushed; no agents, real providers/renderers/publisher, deployment or paid services were enabled.

Final root `.venv/bin/python -m pytest tests/unit tests/integration -m 'not phoenix_live' -q`:754passed,1explicit live test deselected in39.75s, no warnings. Locked offline sync74resolved/70checked and diffcheck passed. Earlier fix covering run547passed+1corrected targeted case and all3split roundtrips are superseded by this fresh complete run.

Fresh standalone `python -m app.demo --offline --output data/foundation-acceptance-2026-11-16`:PASS/synthetic_foundation/schema1.1/AWAITING_APPROVAL. Root independently reloaded9member files/current manifest/runner output/all3bundle+completeoffline exports and verified hashes, negative gates and preserved outbox. Local evidence verifies54evaluator children and10execution spans with correct coverage. Each split contains3executions,18checks,2of2critical outcomes observed and0falseaccepts. Manifest5ba7902f21cff3082904170015a73f229681d9af0e1178944afa02791bbff9d1. Bundle hashes:

- devf4051213e6d151a358831557cfadc2c018316c705d9eec883057b503308eb5aa
- holdout90dc1fafcb1b8a0e3a44f511e3d8eef8fb5a31cb547b45f01bfd79d15018203f
- prod_regression065fc584aecd31990f49c5b7ea6fb013107896ec0bcfdd8a5097b19e0ed89cdb

Changes include durable default approval tracing, explicit degraded/missing trace linkage, actual evaluator child spans/errors, persisted diagnostic bundles with separate configured/observed producers, retry-level usage with unknown propagation, observed/expected critical coverage and all5minor fixes. Approval telemetry and business audit commit independently; orphan attempted-decision spans may remain after later business rollback, while immutable committed receipts establish decisions.

Compatibility adjudication: schema1.1 requires fresh experiment evidence. An approved1.0 registry cannot promote1.1 through the present flow; use a separately governed migration or separate fresh registry with explicit human bootstrap, preserving archived history. No user registry was rebuilt. The runbook's DemoReport1.0 line is stale; the tested producer/contract is1.1. These residuals were disclosed and parked after the single-wave scoped review; no second fix wave was opened.

Other explicit limits: actual GitHub browser OAuth, current live Phoenix API/UI and remoteCI remain unverified. Historical Task9 live/UI evidence is separate. Phoenix21 gives one owning project per globally unique traceID; requested resource tags do not prove ownership. Real editorial/LLM quality and futureagent dashboards require separate slices. InitialDDL hardening applies to fresh schemas; dryrun activeWAL protectsall/unavailable. No operational cleanup or automatic approval occurred.

The user-requested local history, compact resume, ledger and rulings remain under ignored data/execution-history and must survive SDDscratch cleanup. Per-task and final review evidence is retained in Git. Integration choice belongs to the user.
