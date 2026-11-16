# Whole-branch review: 3b210ee → e9e95bf

**Verdict: with fixes; not ready to integrate as complete.** No critical publication or release bypass found in the reviewed foundation paths. Important evidence failures below are reachable in the local foundation, without a live provider. They should be addressed together, with a scoped re-review.

The full-branch package was reviewed in passes using the approved design and the checkout's corresponding source, tests and operational documentation. Strengths: content decisions reverify a live allowlisted session inside the write lock, compare expected manifest/version, reread the registered manifest and members, check current evidence/rights, and commit the receipt, event and state together. Replays return the original receipt and do not reinstate approval after rework. Release approvals have their own version/head, re-evaluation and bootstrap boundary. The runner fails closed for production; there is no publishing path. Registration, interrupted execution, bounded outbox and replay have separate durability boundaries. Holdout remote payloads are reduced, and CI selects only the aggregate artifact. None of this establishes real editorial quality.

## Important findings for the single fix wave

1. **The default browser decision records a trace ID for a span that does not exist.** `app/review/service.py:45,53,75,91` uses the global OTel provider; the application's durable providers are private to `Telemetry._tracer` (`app/observability/tracing.py:266`). With normal wiring the global tracer is a no-op and the receipt gets a fresh random identifier instead. The trace test (`tests/integration/test_approval.py:181`) installs an unrelated global in-memory exporter. Route the actual decision through the sanitized local telemetry service; allow only the opaque decision ID/event, not session data or notes. Never invent a trace ID on failure; make missing/degraded evidence explicit without undoing a valid human decision. Verify receipt-to-span resolution via the normal offline outbox, and historical replay without a duplicate decision event.
2. **Evaluator executions have no evaluator spans.** `app/evals/run.py:175,186` invokes each evaluator after the runner span has ended, without creating a child linked to the evaluated invocation. The design specifies evaluator children; `app/demo.py:236,249` checks runner/model execution coverage only. A rubric error has no evaluator timing or trace event, even though the exported execution coverage can be complete. Instrument the actual evaluation call, including errors, with sanitized metadata, explicitly continuing the invocation's context; verify parent IDs and coverage on a real offline experiment, with no holdout labels or bodies in the spans. Gate re-computation is distinct from the original experiment.
3. **A recorded incomplete experiment can fail persistence and lose the diagnostic evidence.** `app/evals/run.py:203,239–245,289`: the manifest records configured evaluator versions; the validator requires exact equality to observed result producers if any required evaluator is missing. When another configured evaluator emits zero rows (or execution fails before evaluators run), the recorded set is larger than the observed set, so even the honest ERROR/INCOMPLETE bundle cannot be saved. Preserve configuration vs result coverage separately and reject results from unconfigured evaluators. Add a round-trip regression with compound missing/zero-output evaluators, preserving fail-closed gate behavior. This resolves deferred minor 2 as **important**.
4. **Experiment usage reports only the last retry.** `app/evals/run.py:169–174,195–214` collects all attempt times and retry count but derives bundle usage only from each final output. `app/execution/runner.py:164,184,192` allocates a fresh usage accumulator per attempt. A timed-out model call with unknown usage followed by a measured successful retry can thus appear fully measured, and measured earlier attempts can be omitted. Preserve retry-level usage evidence, aggregate all attempts without double counting, and keep totals unknown when a contributing attempt lacks measurements. Verify the persisted and reloaded bundle, not just the monitoring projection.
5. **A missing critical-outcome sample looks like a measured zero in routine eval reports.** `app/evals/run.py:102–108` counts only observed false accepts, and `app/evals/models.py:242,251` always reports the integer as the total. A failed/missing critical rejection example with no output can display zero without identifying missing critical coverage. The gate still blocks, but the monitoring projection already treats incomplete coverage as unknown. Distinguish observed count from known total/coverage in persisted or public summaries, including the demo aggregate, and regression-test an unexecuted critical example. Do not lose known observed false accepts when other rows are missing.

## Remaining deferred findings

- **Fix (minor):** Task 4, `tests/integration/test_state.py:12–17`: replace the duplicated enum slicing and logic with named, independently specified allowed edges. Keep explicit forbidden approval/publication assertions.
- **Fix (minor compatibility):** Task 12, `app/web.py:19–20`: preserve the prior third positional clock in Services; make review keyword-only or append it. The current layout silently redirects a working embedder's clock argument into the review service.
- **Fix (minor test portability):** Task 13, `tests/unit/test_monitoring.py:19,152,303`: use a deterministic runner clock or derive query cutoffs from actual records for eval-window tests. Tests currently mix fixed and wall-clock timestamps.
- **Fix (minor test coverage):** Task 14, `tests/integration/test_retention.py:90–98`: use a valid canonically named and hashed incomplete experiment; assert protection specifically for completion_unproven. The existing invalid name never enters the completion check.
- **Fix (minor retention correctness):** Task 14, `app/maintenance.py:148–151`: exclude empty reference strings before path resolution. An ordinary empty review note resolves to the current directory and protects unreferenced bundles below it. Preserve support for actual relative references; this bug overprotects rather than deleting data.

## Ledger rulings — disposition and costs

Reviewed all 15 Ruling lines in progress.md, in order. Retain each for the final user report. None justifies omitting the fixes above.

1. Accept tested 3.12 only; other supported minors need independent validation.
2. Accept isolated checkout; original checkout will not contain the work without separate integration.
3. Accept removing indexed .DS_Store metadata; original files remain untouched.
4. Accept independent storage/evidence work with serialized integration; risk was contract drift, covered by integrated checks.
5. Accept trusted stage-policy registry; future agent validators must be supplied, or that agent remains blocked.
6. Accept explicit store-bound readiness; adapter migration cost, no unchecked artifacts.
7. Accept locked local venv and temp cache; environment drift remains bounded by locked sync.
8. Accept HTTP loopback-only collector config; remote/TLS configurations are unsupported.
9. Accept explicit Stage.output_type and persisted trusted handoff; future stages must implement this protocol.
10. Accept observed example execution timestamps; additive contract/consumer cost.
11. Accept Phoenix single owning project per globally unique trace; project tags are requested, actual cross-project ownership/navigation is not promised; future topology work required.
12. Accept default-deny release verifier; missing wiring prevents approval. This does not make a global no-op tracer a durable provider.
13. Accept pre-release fresh-DB DDL hardening; existing development DBs do not acquire the protections automatically.
14. Accept dry-run protecting everything with an active WAL, and protecting pending telemetry/whole offline exports; possible retention/storage growth. Supported authority layouts only; the runbook explicitly excludes external references.
15. Accept observing outbound transport during local approval; it does not test any future publishing adapter.

## Declined to judge / limits

- No live GitHub browser sign-in, live Phoenix rerun, or hosted CI run: those were not authorized here. Mocked GitHub plus real SQLite, historical Phoenix probe, current offline fallback, and local CI-definition checks are different evidence.
- No real agent/provider/editorial calibration, renderer, publisher, deployment, or multi-machine durability claim: intentionally excluded. Human content approval and model-release promotion are distinct; the synthetic demo may stop in review without an active model release.
- No administrator-tampering or uncooperative filesystem writers guarantee; the runbook limits retention authority to the supported layout. Old scratch databases require fresh schema evidence. No durable completion record currently justifies cleanup of queued telemetry or whole offline exports.
- Declined no other behavior as out of scope.

**Validation:** read-only integration review, no new test run or service calls. Controller-supplied fresh evidence: 741 offline tests passed, 1 live deselected; independent synthetic demo PASS at AWAITING_APPROVAL, 3 splits/54 checks and 10/10 runner invocation pairs. These establish normal-path behavior, not the exception and tracing cases above. Only this review report was written.


# Scoped re-review of the consolidated fix

**All ten original findings are addressed.** The changes preserve the authenticated approval and release boundaries. One compatibility limitation affects a registry containing an approved schema 1.0 experiment. The controller should disclose it when presenting integration options.

| Original finding | Verdict | Evidence reviewed |
| --- | --- | --- |
| Important 1. Decision traces | ADDRESSED | The default ReviewService uses the local sanitized SDK/outbox and records the accepted trace ID in the receipt and state event. Failed local enqueue yields a null ID and DEGRADED. Receipt replay precedes emission. The tests exercise the default wiring and business rollback. |
| Important 2. Evaluator spans | ADDRESSED | Each actual required evaluator call continues its output invocation's trace and span IDs. Returned errors and exceptions have sanitized signals. The offline demo compares the multiset of evaluator IDs and parent links, so even one missing child prevents a pass. |
| Important 3. Incomplete experiment persistence | ADDRESSED | Bundle 1.1 separately binds ordered configured pins and observed producers. Missing evaluators plus zero emitted results or earlier execution failure survive persist/read. Validation rejects unconfigured results and inconsistent evaluator fingerprints. |
| Important 4. Retry usage | ADDRESSED | The producer reads each journaled attempt in retry order. The bundle includes each attempt's usage, verifies the final output usage where available, and propagates an unknown measurement through that field's total. Round-trip cases cover both measured and unmeasured first attempts. |
| Important 5. Unknown critical outcomes | ADDRESSED | The pinned split determines the critical rejection population and offline binding rechecks it. Observed positives remain visible when an outcome is missing. The total becomes null, and release/demo success requires a known zero. |
| Minor 1. Transition oracle | ADDRESSED | All state pairs are checked against named allowed targets independent of production enum slicing. |
| Minor 2. Positional clock | ADDRESSED | Services retains the third positional clock. The review service is keyword-only. |
| Minor 3. Monitoring test dates | ADDRESSED | The fixture runner uses the same deterministic clock as the query window. |
| Minor 4. Incomplete retention fixture | ADDRESSED | A canonically persisted incomplete bundle exercises completion_unproven. The test asserts the separate pending-file reason. |
| Minor 5. Empty path reference | ADDRESSED | The retention path scan excludes empty strings and regression coverage retains actual relative reference protection. |

**Remaining compatibility limitation.** A fresh 1.1 candidate alone cannot upgrade a registry whose approved baseline is 1.0. The new type accepts only 1.1 at app/evals/models.py:237. The registry always decodes the stored historic evidence with that type at app/release/service.py:52, and blocks on invalid_baseline at line 75. Revocation preserves the baseline. Existing receipts/bytes survive, but that registry cannot promote a new candidate through the present approval flow. Fresh synthetic registries are unaffected. This is a fail-closed, pre-release limitation. An operator with an existing approval needs a separately governed migration or a separate fresh registry with explicit human bootstrap, preserving the archived registry. The runbook also still calls DemoReport 1.0 at docs/foundations-runbook.md:27, although the new producer emits 1.1. These are residuals for controller adjudication.

The separate telemetry and business commits in ruling 16 are consistent with the implementation. A later business rollback can leave an opaque attempted-decision span. The immutable business receipt remains the authority for a committed decision. An accepted local span establishes local persistence only.

**Verification scope.** Static review of the supplied working-tree diff and changed call paths. Fixer evidence covered 548 affected cases across the initial run and corrected-case rerun, plus three successful synthetic split round trips with 18 checks and 2/2 critical outcomes each. The controller owns fresh full-suite and independent demo verification. Only this report was written.
