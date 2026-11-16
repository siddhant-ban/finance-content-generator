# Evaluation-first Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove contracts, durable workflow state, Phoenix observability, evaluation gates, and GitHub-authenticated human review before building individual agents.

**Architecture:** One local Python application uses explicit services, SQLite transactions, immutable artifacts, and a deterministic runner. Fake stages/providers exercise the interfaces that later agents will use. A loopback FastAPI service owns authentication and human actions; Streamlit provides read-only review navigation.

**Tech Stack:** Python 3.11+, Pydantic, SQLite, pytest, OpenTelemetry SDK/OTLP, local Phoenix, FastAPI, an OAuth client library, Streamlit. Verify compatible versions and lock dependencies/image digest during the specified tasks; no speculative pins.

**Spec:** `docs/superpowers/specs/2026-11-09-foundations-design.md` (approved 2026-11-09). Also read `AGENTS.md`, `docs/ARCHITECTURE.md`, and `docs/EVALUATION_PLAYBOOK.md`.

## Independent review resolutions — 2026-11-10

The [GPT Astra medium review](../reviews/2026-11-09-astra-foundations-review.md)
identified two P2 findings. Both corrections are incorporated below and were
independently re-reviewed with no remaining important blocker. This table makes
their implementation and regression-test ownership explicit; it does not mark
the future tests as executed.

| Finding | Required implementation | Owning tasks | Required regression evidence |
| --- | --- | --- | --- |
| R1: early evidence gaps could strand stories | Persist and resume the last valid `DISCOVERED`, `CLUSTERED`, or `SCORED` checkpoint; validate corrected inputs and preserve ADVANCE gating | 4 | Parameterize evidence gap → correction → recovery at both early states; no missing-score requirement, skipped stage, or HOLD dispatch |
| R2: approval/create retries lacked an explicit contract | Carry scoped idempotency keys through creation, release forms and both approval service layers; atomically record request fingerprint and original receipt | 4, 10, 12 | Lost-response retry returns the original receipt with exactly one event; changed-payload reuse conflicts; duplicate creation fails; revoked approval never reactivates; replay still requires live allowed authentication |

R1 changes the recovery matrix and Task 4 tests. R2 changes the exact
`StateService.create`, `ReleaseService.approve`, and `ReviewService.release_decide`
signatures, transaction/replay ordering, browser form fields and Tasks 4/10/12
tests. These requirements are part of their respective red/green cycles, not
optional follow-up work.

## Global Constraints

- Python 3.11+; Pydantic contracts; SQLite and local artifacts for the MVP.
- Browser sign-in through GitHub; only the user's account may approve.
- No cloud deployment, paid service, social publishing or recurring spend without explicit user approval.
- Only an authenticated human action can enter `APPROVED_FOR_PUBLISH`.
- `PUBLISHED` is reserved and unreachable in the MVP.
- No material unsupported fact may advance.
- Use at most three attempts total, with exponential backoff bounded at 30 seconds.
- Proposed caps: 10,000 pending span records or 50 MiB, whichever is reached first.
- Exporter shutdown is bounded at 5 seconds.
- Recommended limits are 30 minutes idle and 8 hours absolute session lifetime.
- Retain telemetry/eval bundles 30 days by default; keep live artifact/review/audit lineage until explicit deletion.
- Existing uncommitted files belong to the user and must be preserved.
- No individual agent, real model adapter, renderer, or publishing adapter is implemented by this plan.

## Review Focus

1. Simultaneous approval and edits must never leave stale approval active (Tasks 4/12).
2. Storage/outbox crashes must preserve the last valid state and report degradation (Tasks 3/5/6/15).
3. Missing labels, judges, split members, baselines, or measurements must never become successful evals or zero cost (Tasks 7/8/10).
4. Account switching, forged browser data, stale sessions, and CSRF must never impersonate the allowed human (Tasks 11/12).
5. Telemetry must not retain credentials, reviewer identity, or copyrighted text, including error paths (Tasks 5/9/15).

## Execution protocol and milestones

This is a plan, not evidence of executed tests. Commands and expected outcomes below are future acceptance checks. Each task requires demonstrated red/green, spec-compliance review followed by code-quality review, a short report, and a scoped commit. Critical findings block progression. Use `test-driven-development`, `requesting-code-review`, and `verification-before-completion` during execution. Delegate only stable independent tasks; most tasks here are sequential.

At execution start use `using-git-worktrees` for isolation and a `codex/` branch. The current baseline contains untracked design inputs: preserve/include the reviewed inputs in the isolated checkout without overwriting or staging unrelated files. Do not blindly create a worktree missing those inputs. Stage only exact task paths, never all files.

Create/update `TASKS.md` per task: status, branch, owner, test command/result, eval command/result, review findings, trace link or offline bundle, known gaps. A layer not built yet is `not applicable yet` with its dependency, never an implied pass. Intermediate tasks are not production-ready agent slices. Milestones must pass all accumulated checks before progressing.

| Milestone | Tasks | Deliverable |
| --- | --- | --- |
| A | 1–4 | Durable validated contracts/state/artifacts; approval and publication blocked |
| B | 5–6 | Observable fake-stage run; outage, overflow and restart proven |
| C | 7–10 | Synthetic evals, Phoenix/offline bundles, fail-closed promotion eligibility |
| D | 11–15 | Authenticated synthetic review, revocation, monitoring and full acceptance |

## File map

Use root `app/` to preserve the playbook's `python -m app...` commands. Create package markers with the first module. No `app/agents/` package is created.

| Paths | Responsibility / task |
| --- | --- |
| `pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `.gitignore` | Runtime/dependency locks and safe defaults (1; additions 5/11/12) |
| `app/config.py`, `app/contracts/{common,execution,evidence,workflow,export}.py`, `schemas/v1/*.json` | Settings, typed boundaries and schema snapshots (1/2) |
| `app/prompts.py`, `prompts/manifests/foundation.v1.json`, `prompts/foundation/fake_stage.v1.md` | Immutable synthetic prompt (1) |
| `app/storage/{db,artifacts}.py`, `app/storage/migrations/001_core.sql` | Artifacts and migrations (3) |
| `app/workflow/{state,policy}.py`, `app/storage/migrations/002_workflow.sql` | State, audit, rework and idempotency (4) |
| `app/observability/{sanitize,outbox,tracing,health}.py`, `app/storage/migrations/003_telemetry.sql` | Durable sanitized traces (5) |
| `app/execution/{protocols,runner,fakes}.py`, `app/storage/migrations/004_attempts.sql` | Stage/provider runner and attempts (6) |
| `app/evals/{models,datasets,evaluators,run,compare,phoenix}.py` | Governance, experiments, registration (7–9) |
| `app/release/{models,check,service}.py`, `app/storage/migrations/005_releases.sql` | Eligibility vs human release approval (10/12) |
| `app/auth/{github,sessions,dependencies}.py`, `app/web.py`, `app/storage/migrations/006_auth.sql` | OAuth/session authority (11) |
| `app/review/{service,routes}.py`, `app/ui/review.py`, `app/storage/migrations/007_review.sql` | Manifest-bound review and dashboard (12) |
| `app/monitoring/{queries,ui}.py`, `app/maintenance.py`, `app/demo.py`, `.github/workflows/ci.yml` | Monitoring (13), retention (14), demo/CI (15) |
| `tests/{unit,integration,fixtures}/`, `tests/conftest.py` | Offline fixtures and injected dependencies |
| `evals/foundation/1.0.0/`, `evals/policies/foundation.v1.json`, `evals/golden/manifest.json` | Synthetic evals and starter exclusion (7/8) |
| `docs/{dependencies,foundations-runbook}.md`, `TASKS.md` | Compatibility, reproduction, evidence ledger |

## Shared names and serialization

IDs are UUIDs; hashes are lowercase SHA-256. UTC datetimes serialize with `Z`; reject naive dates, normalize aware offsets. Decimal values serialize as strings. Strict Pydantic parsing rejects arbitrary coercions/extra fields; JSON date/decimal parsing remains explicit. `schema_version="1.0"`; reject unsupported major versions. Hash canonical sorted-key UTF-8 JSON with decimal strings and UTC dates.

`ArtifactRef` has hash, media type, size and schema version; only the store resolves paths. `Principal` has provider, numeric account ID, principal kind, and server-side session ID. Browser-supplied actor fields never create an authenticated principal. `PolicyReport` has `ready: bool`, named check results, evidence revision refs and critical failures, recomputed from stored artifacts by trusted code.

`InputEnvelope[T]` carries run/story/invocation IDs, typed input/ref, schema, agent/prompt/config versions/hashes, and optional W3C parent context. `OutputEnvelope[T]` carries identifiers, typed payload/ref or typed error, `OK|FAILED|BLOCKED`, warnings, evidence refs, usage, trace/span IDs. `Usage` has nullable token counts, separately nullable measured/estimated money, and availability reason. `ObservabilityStatus` is `HEALTHY|DEGRADED`, separate from execution and editorial status.

### State matrix

No unlisted edge is allowed. Every mutation requires expected version, caller-scoped idempotency key, immutable refs, and same-transaction audit. Agents submit stage results, never authoritative state writes.

| From | To | Condition |
| --- | --- | --- |
| absent | `DISCOVERED` | Unique story and valid candidate ref |
| `DISCOVERED` | `CLUSTERED` | Valid cluster artifact |
| `CLUSTERED` | `SCORED` | Valid score artifact/arithmetic |
| `SCORED` | `RESEARCHED` | ADVANCE decision, credibility eligibility, valid dossier |
| `RESEARCHED` | `VERIFIED` | All material current claim revisions verified; caveats accounted for |
| `VERIFIED` | `SCRIPTED` | Factual sentences cite approved revisions; caveats retained |
| `SCRIPTED` | `RENDERED` | Valid render/chart/subtitle/license checks |
| `RENDERED` | `AWAITING_APPROVAL` | Complete current review manifest and readiness |
| `AWAITING_APPROVAL` | `APPROVED_FOR_PUBLISH` | Authenticated allowed human, exact current manifest (Task 12 only) |
| Any of the nine pre-publication states above | `REJECTED` | Credibility failure or authenticated human rejection |
| Any of those nine states | `NEEDS_RESEARCH` | Evidence gap; revoke approval, save checkpoint/reason/resume target |
| Any of those nine states | `NEEDS_REVISION` | Correctable failure/edit; revoke approval, save checkpoint/reason/resume target |
| `NEEDS_RESEARCH` | Saved checkpoint in `DISCOVERED,CLUSTERED,SCORED` | Corrected inputs revalidated; only saved checkpoint; early stories require no nonexistent score; downstream dispatch still requires eligible ADVANCE |
| `NEEDS_REVISION` | Saved checkpoint in `DISCOVERED,CLUSTERED,SCORED,RESEARCHED,VERIFIED,SCRIPTED,RENDERED` | Only persisted target; revalidate retained inputs and invalidate downstream |
| `NEEDS_RESEARCH`, `NEEDS_REVISION` | `REJECTED` | Credibility failure or human rejection |
| `AWAITING_APPROVAL`, `APPROVED_FOR_PUBLISH` | `AWAITING_APPROVAL` | Review-copy-only change; new ready manifest, revoke old approval |
| `PUBLISHED`, `ANALYZED`, `REJECTED` | none | Immutable/terminal in foundations |

When entering `NEEDS_RESEARCH`, save the last valid early checkpoint: `DISCOVERED` if no valid cluster exists, `CLUSTERED` if no valid score exists (or the score is invalidated), otherwise `SCORED`. Invalidate research and later artifacts. Correction resumes only that checkpoint after validating its corrected immutable inputs; a valid retained HOLD score still cannot dispatch. Test evidence gaps originating at both `DISCOVERED` and `CLUSTERED`, followed by correction, resumed normalization/scoring and normal gated advancement.

`HOLD` stays `SCORED` without dispatch. Derive invalidation from changed member hashes, never a caller's `ChangeKind` assertion. Evidence/claim/source-data changes resume from `SCORED` and rebuild research. All script/narration/caption/subtitle-text/chart-data changes conservatively resume from `RESEARCHED` (or earlier if evidence changed) and reverify; do not attempt an LLM factual-versus-phrasing classifier in the MVP. Layout/timing-only changes with unchanged textual/data hashes resume from `SCRIPTED`. The manifest-only edge requires identical publication member hashes and changed internal review metadata only. All new manifests revoke old approval. Future `APPROVED_FOR_PUBLISH → PUBLISHED → ANALYZED` stays denied, including direct SQL.

---

### Task 1: Strict contracts and reproducible baseline

**Files:** Create `pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `app/__init__.py`, `app/config.py`, `app/contracts/{common,execution,export}.py`, `app/prompts.py`, `schemas/v1/{common,execution}.json`, `prompts/manifests/foundation.v1.json`, `prompts/foundation/fake_stage.v1.md`, `tests/conftest.py`, `tests/unit/test_{contracts,config,prompts}.py`, `docs/dependencies.md`, `TASKS.md`. Modify `.gitignore` for runtime data/telemetry/session files, preserving schemas/fixtures.

**Interfaces:** Produce `Settings.from_env() -> Settings`, `InputEnvelope[T]`, `OutputEnvelope[T]`, `ArtifactRef`, `Usage`, `Principal`, `Money`, distinct `Percent`/`PercentagePoints`, `decode_envelope(data: bytes, payload_type: type[T]) -> InputEnvelope[T]`, `load_prompt(manifest_path: Path) -> PromptSpec`. PromptSpec pins ID/version/path/hash. Config hashes exclude secrets. Defaults disable publication, paid providers and fake stage outside tests/demo.

- [x] **Write failing tests:** `test_unknown_major_and_extra_fields_rejected`, `test_naive_date_rejected_offset_normalized`, `test_percent_not_percentage_points`, `test_failed_envelope_requires_error`, `test_unknown_usage_is_null_not_zero`, `test_prompt_hash_mismatch`, `test_safe_config_defaults`, `test_secrets_not_in_repr`. Assert validation errors, exact UTC representation, no mixed success/error payload, and fail-closed defaults.
- [x] **Demonstrate red:** create only minimal packaging/test dependency setup; run `uv run pytest tests/unit/test_contracts.py tests/unit/test_config.py tests/unit/test_prompts.py -q`; expected missing contract imports. An unavailable interpreter/dependency is setup failure, not demonstrated red.
- [x] **Implement minimum:** verify primary dependency docs, choose available supported Python 3.11+ minor, record compatibility in `docs/dependencies.md`, lock Pydantic/pytest versions. Do not assume observed Python 3.15 support. Implement models/settings and schema snapshot export `python -m app.contracts.export --out schemas/v1` in `app/contracts/export.py`. Synthetic prompt is immutable v1. `.env.example` has placeholders only.
- [x] **Demonstrate green:** same pytest command exits 0; `uv sync --locked` succeeds; schema regeneration matches checked-in snapshots and prompt hash check passes. Record selected minor in `.python-version` and tested supported range in `pyproject.toml`.
- [x] **Review/report/commit:** `feat: establish strict foundation contracts`; evals/traces are not applicable yet (Tasks 5/7). Stage only exact task paths.

### Task 2: Evidence revisions and readiness contracts

**Files:** Create `app/contracts/{evidence,workflow}.py`, `schemas/v1/{evidence,workflow}.json`, `tests/unit/test_evidence.py`, `tests/fixtures/evidence/{valid,missing_publication,conflicting,revised}.json`.

**Interfaces:** Consume Task 1 types. Produce `SourceRecord`, `ClaimRevision`, `EvidenceRef`, `VerificationRecord`, `ReviewManifest`, `PolicyReport`, `StoryState`, `ChangeKind`, `validate_evidence(claims: list[ClaimRevision], sources: list[SourceRecord], verdicts: list[VerificationRecord]) -> PolicyReport`. Sources record original/canonical URL, class/publisher, retrieval/publication time, missing-date reason, locator/excerpt, rights/hash. Claims bind stable ID/revision, type/materiality, exact text/caveats, numeric observations and evidence revisions. ReviewManifest binds all script/dossier/verification/render/subtitle/caption/chart/attribution/license inputs.

- [x] **Write failing tests:** `test_unknown_publication_requires_reason`, `test_unknown_rights_disallows_excerpt`, `test_numeric_basis_vintage_units_explicit`, `test_stale_source_cannot_verify_current_claim`, `test_missing_material_verdict_blocks`, `test_each_use_preserves_caveat`, `test_analysis_label_does_not_exempt_facts`. Assert orphan refs and incomplete manifests fail; honest unknown dates and unsupported claims remain representable but not ready.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_evidence.py -q`; expected missing models/validators.
- [x] **Implement minimum:** strict models and deterministic referential validation, including currency, percent/percentage-point, nominal/real, forecast/actual, period/geography and revision basis. No network or model memory verifies evidence. A hash/reference match is never semantic entailment.
- [x] **Demonstrate green:** same command and accumulated `uv run pytest tests/unit -q` exit 0; schema snapshots match.
- [x] **Review/report/commit:** `feat: version evidence and review contracts`; document semantic verification as future fact-checker/human work.

### Task 3: Immutable artifacts and migration recovery

**Files:** Create `app/storage/{db,artifacts}.py`, `app/storage/migrations/001_core.sql`, `tests/integration/test_storage.py`.

**Interfaces:** Produce `open_database(path: Path) -> sqlite3.Connection`, `migrate(conn: Connection) -> None`, `ArtifactStore.put(data: bytes, media_type: str, schema_version: str) -> ArtifactRef`, `ArtifactStore.read(ref: ArtifactRef) -> bytes`, `ArtifactStore.reconcile() -> RecoveryReport`. RecoveryReport lists missing/corrupt references and orphan files without deletion. DB stores immutable artifact metadata and migration versions.

- [x] **Write failing tests:** `test_same_bytes_same_ref`, `test_temp_write_crash_never_registers_missing_file`, `test_rename_before_db_crash_reports_orphan`, `test_corrupt_ref_blocks_handoff`, `test_path_traversal_and_escaping_symlink_denied`, `test_migrations_atomic_idempotent`, `test_foreign_keys_wal_and_timeout`. Inject faults around file/DB commits; assert prior committed bytes survive.
- [x] **Demonstrate red:** `uv run pytest tests/integration/test_storage.py -q`; expected missing storage interface.
- [x] **Implement minimum:** validated hash-derived paths, temp write/flush/fsync/atomic rename before DB registration. Enable foreign keys, WAL and 5,000 ms busy timeout. Never resolve caller paths or remote URLs. Recovery reports missing content without fabricating it.
- [x] **Demonstrate green:** same command exits 0 against reopened on-disk SQLite, not only in-memory DB.
- [x] **Review/report/commit:** `feat: persist immutable artifacts with crash recovery`; record disk-full and local machine-admin threat boundaries.

### Task 4: Transactional state, rework and invalidation

**Files:** Create `app/workflow/{state,policy}.py`, `app/storage/migrations/002_workflow.sql`, `tests/unit/test_policy.py`, `tests/integration/test_state.py`.

**Interfaces:** Consume Tasks 2/3. Produce `TransitionRequest`, `StateEvent`, `StorySnapshot`, `StateService.create(story_id: UUID, run_id: UUID, candidate: ArtifactRef, caller_scope: str, idempotency_key: str, expected_version: int = 0) -> StorySnapshot`, `StateService.transition(request: TransitionRequest) -> StorySnapshot`, `StateService.revise(story_id: UUID, manifest: ReviewManifest, change: ChangeKind, expected_version: int, idempotency_key: str) -> StorySnapshot`, `evaluate_readiness(manifest: ReviewManifest, now: datetime) -> PolicyReport`. StateEvent includes architecture audit fields plus version/hash/idempotency. Approval transaction remains internal/unavailable until Task 12.

- [x] **Write failing tests:** parameterize all state pairs against the matrix; `test_agent_cannot_approve_or_publish`, `test_actor_string_not_identity`, `test_direct_sql_forbidden_edges_and_audit_mutation_denied`, `test_idempotency_replay_and_payload_conflict`, `test_two_connections_one_version_wins`, `test_revision_only_saved_checkpoint`, `test_early_evidence_gap_corrects_and_resumes`, `test_research_resume_does_not_bypass_advance`, `test_create_retry_same_request_one_event_changed_payload_conflicts`, `test_edit_revokes_approval_atomically`, `test_factual_caption_reverifies`, `test_forged_cosmetic_change_kind_cannot_keep_verification`, `test_hold_no_dispatch`. Assert event/state rollback together and restart reloads identical versions. Before real auth, only a private test fixture can insert approval decisions.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_policy.py tests/integration/test_state.py -q`; expected missing workflow service.
- [x] **Implement minimum:** explicit matrix, optimistic updates, scoped immutable request fingerprints, transactional audit and DB triggers rejecting forbidden edges/audit mutation. SQL approval update requires matching current decision record; authentication remains an application responsibility. Recompute readiness from stored current artifacts, never client assertions.
- [x] **Pin creation replay semantics:** `expected_version=0` means the story must be absent for a new create. Atomically store the original response/event under `(caller_scope, operation, idempotency_key)` with a fingerprint covering run/story IDs, candidate hash and expected version. Identical replay returns the original response with no second event, even if the story later progressed; changed-payload reuse conflicts. A different key cannot recreate an existing story.
- [x] **Demonstrate green:** same command and `uv run pytest tests/unit tests/integration -q` exit 0, including race/restart tests.
- [x] **Review/report/commit:** `feat: enforce audited workflow transitions`; Milestone A report names missing auth, tracing and eval layers rather than claiming full slice readiness.

### Task 5: Sanitized durable OpenTelemetry and outage handling

**Files:** Create `app/observability/{sanitize,outbox,tracing,health}.py`, `app/storage/migrations/003_telemetry.sql`, `tests/unit/test_sanitize.py`, `tests/integration/test_telemetry.py`. Modify `pyproject.toml`, `uv.lock`, `.env.example`, `observability/docker-compose.yml`, `observability/README.md`, `docs/dependencies.md`.

**Interfaces:** Produce `sanitize_attributes(attributes: Mapping[str, object]) -> dict[str, object]`, `Telemetry.enqueue(record: SpanRecord) -> EnqueueResult`, `Telemetry.flush(timeout_seconds: float = 5) -> ExportReport`, `Telemetry.health() -> HealthReport`, `Telemetry.story_context(run_id: UUID, story_id: UUID, parent: str | None) -> ContextManager`, `Telemetry.span(name: str, project: str, attributes: Mapping[str, object]) -> ContextManager`. SpanRecord is a serializable allowlisted span with stable OTel IDs/parent, project, timestamps, sanitized attributes/events, and export status. Expose pending count/bytes, lost records and run degradation; never embed business IDs as trace IDs.

- [x] **Write failing tests:** `test_nested_errors_urls_and_events_redacted`, `test_context_across_projects_and_restart`, `test_failed_export_keeps_pending`, `test_capacity_bytes_rows_atomic`, `test_overflow_preserves_queue_marks_run_degraded`, `test_disk_failure_surfaces`, `test_replay_preserves_ids`, `test_flush_five_second_budget`. Seed token/cookie/password/private-name/article-body sentinels; inspect raw DB/outbox/export bytes for absence. Fill queue to both limits separately; assert next enqueue fails explicitly, prior records remain, lost count increments, and run status is degraded.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_sanitize.py tests/integration/test_telemetry.py -q`; expected missing telemetry modules.
- [x] **Implement minimum:** official OTel SDK interfaces, durable sanitized queue, injected transport/clock, background export outside state transactions. Caps are 10,000 records or 52,428,800 serialized payload bytes; never evict pending entries. Preserve IDs through at-least-once replay; deduplication is verified later, not assumed. Whitelist safe parameters/error codes, not raw exception strings. Suppress automatic HTTP logging/instrumentation of OAuth callbacks. DB failure must reach run report even if outbox itself cannot record it.
- [x] **Pin and demonstrate green:** verify primary Phoenix/OTel docs, pin SDK/exporter and Phoenix image digest. Bind compose ports to `127.0.0.1`; preserve volume, document tested protocol/port and health check. Same pytest command exits 0; offline sanitized span export is tested. No Docker startup in default tests; live check is Task 9.
- [x] **Review/report/commit:** `feat: add durable sanitized tracing`; report actual offline bundle/trace IDs and known duplicate-delivery behavior.

### Task 6: Typed fake-stage runner, provider boundary and attempt journal

**Files:** Create `app/execution/{protocols,runner,fakes}.py`, `app/storage/migrations/004_attempts.sql`, `tests/unit/test_runner.py`, `tests/integration/test_execution_recovery.py`. Modify `app/contracts/execution.py` and its schema for pinned attempt metadata if required.

**Interfaces:** Consume Tasks 1–5. Produce `Stage[I,O]` protocol with `agent_name`, `agent_version`, `prompt_id`, `prompt_version`, `validate_input(value: I) -> None`, `run(value: I, context: RunContext) -> O`, `validate_output(value: O) -> None`; `Provider.generate(request: ModelRequest) -> ModelResponse`; `AgentRunner.run(stage: Stage[I,O], envelope: InputEnvelope[I]) -> OutputEnvelope[O]`; `AgentRunner.recover_interrupted() -> list[AttemptRecord]`. RunContext exposes safe tool/provider spans but no approval capability. ModelRequest/Response pin provider/model/parameters, typed content/usage and availability. FakeStage transforms synthetic typed data; FakeProvider emits configurable responses/errors, never a remote request.

- [x] **Write failing tests:** `test_success_has_typed_refs_versions_trace`, `test_timeout_three_attempts_stable_run_distinct_invocations`, `test_schema_policy_failure_no_retry`, `test_unknown_usage_not_free`, `test_crash_after_output_before_transition_no_double_advance`, `test_interrupted_attempt_explicit_on_restart`, `test_telemetry_down_execution_continues_degraded`, `test_persist_failure_no_state_advance`, `test_fake_provider_cannot_publish`. Assert exact metadata from spec, one root/story trace with attempt children, state update only after stored validated output, and `retry.count` is attempts minus one.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_runner.py tests/integration/test_execution_recovery.py -q`; expected missing runner/protocols.
- [x] **Implement minimum:** injected clock/sleeper for deterministic retry tests, initial delay 1 s doubled up to 30 s, maximum three attempts total. Only transient provider/network errors retry; schema/policy failures return typed blocked/failed outcomes. Journal attempt start/output/completion and input fingerprint; recover interrupted attempts without assuming an external call was exactly-once. State handoff uses Task 4 idempotency. Freeze configuration/prompt hashes per run.
- [x] **Demonstrate green:** same command and accumulated `uv run pytest tests/unit tests/integration -q` exit 0 without Ollama, GitHub or network. Required run metadata and context appear in fake exporter/offline bundle.
- [x] **Review/report/commit:** `feat: run observable typed stages with bounded retries`; Milestone B proves infrastructure only, not editorial agent performance.

### Task 7: Immutable datasets, split governance and illustrative exclusion

**Files:** Create `app/evals/{models,datasets}.py`, `evals/foundation/1.0.0/{manifest.json,dev.jsonl,holdout.jsonl,prod_regression.jsonl}`, `evals/golden/manifest.json`, `tests/unit/test_datasets.py`. Modify `evals/golden/README.md` to link the synthetic dataset without modifying illustrative example rows.

**Interfaces:** Produce `DatasetManifest`, `EvalExample`, `LoadedDataset`, `load_dataset(root: Path, split: Literal['dev','holdout','prod_regression'], purpose: Literal['tune','evaluate','gate']) -> LoadedDataset`. Manifest pins agent/version, immutable per-file/content hashes, dataset kind `synthetic_foundation|illustrative|adjudicated_agent`, input/reference schemas, split memberships/event groups, evidence fingerprints, rights, adjudication and label taxonomy. Examples include the playbook fields plus event group and rights metadata. Hashes cover all split files; validate cross-split leakage globally before selecting a split.

- [x] **Write failing tests:** `test_duplicate_example_ids`, `test_event_or_evidence_leaks_across_splits`, `test_file_hash_changed_version_rejected`, `test_empty_required_split_blocks_gate`, `test_missing_adjudication_or_rights`, `test_illustrative_never_promotable`, `test_holdout_tune_denied`, `test_holdout_report_contains_no_examples`. Assert specific validation errors; malformed input never silently skips a row. Agent datasets require human adjudication; synthetic datasets are explicitly labeled fixture evidence, not editorial truth.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_datasets.py -q`; expected missing dataset loader.
- [x] **Implement minimum:** JSONL loader with line-numbered safe errors; immutable hashes and event/near-duplicate evidence grouping validation. Reject unknown split/agent and unresolved reference IDs when required. Synthetic examples cover valid/invalid refs, state gates, changed versions, missing metadata and critical traps; at least two examples in each split, distinct event groups, at least one critical rejection per split. Add a manifest marking original starter JSONL illustrative/unadjudicated.
- [x] **Demonstrate green:** same command exits 0. Independently reread full dataset files to confirm fixture split groups differ; validate no production tuning command accepts holdout. Do not treat these foundation numbers as a sufficient real-agent sample size.
- [x] **Review/report/commit:** `feat: govern versioned evaluation datasets`; report dataset version/hash/counts and synthetic limitation.

### Task 8: Evaluators, experiment bundles and deterministic CLI

**Files:** Create `app/evals/{evaluators,run,compare}.py`, `evals/policies/foundation.v1.json`, `tests/unit/test_evaluators.py`, `tests/integration/test_eval_cli.py`, `tests/fixtures/evals/{judge_calibration,approved_baseline}.json`. Modify `app/evals/models.py` with evaluator/experiment models.

**Interfaces:** Produce `EvalResult` (`PASS|FAIL|ERROR|SKIPPED`, metric/value/denominator/severity/reason), `Evaluator.evaluate(example: EvalExample, output: OutputEnvelope) -> list[EvalResult]`, `RubricEvaluator` with judge/prompt/version/calibration refs, `ExperimentConfig`, `ExperimentBundle`, `run_experiment(config: ExperimentConfig, dataset: LoadedDataset, runner: AgentRunner, evaluators: list[Evaluator]) -> ExperimentBundle`, `compare(candidate: ExperimentBundle, baseline: ExperimentBundle) -> ComparisonReport`. Bundle binds all spec provenance, per-example results, repeat count/seed, aggregates/sample sizes, usage availability and trace refs; output is immutable by content hash.

- [x] **Write failing tests:** `test_one_critical_failure_beats_high_mean`, `test_judge_timeout_error_not_zero_score`, `test_skipped_required_check_incomplete`, `test_missing_denominator_not_success`, `test_bundle_provenance_and_reproducible_deterministic_scores`, `test_model_cost_missing_stays_unknown`, `test_mismatched_dataset_policy_not_comparable`, `test_cli_exit_codes_and_holdout_redaction`. Fake judges validate harness behavior only. Assert empty/incomplete run cannot produce PASS, and comparison includes failing examples privately with redacted public holdout output.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_evaluators.py tests/integration/test_eval_cli.py -q`; expected missing evaluation/CLI functions.
- [x] **Implement minimum:** deterministic contract/evidence/state/metadata checks and calibrated-rubric adapter interface. Foundation policy requires contract/policy pass rate 1.0, critical false accepts 0, fake-rubric harness check pass, all three splits nonempty and complete. Real agents cannot reuse the fake rubric. Persist code commit plus dirty diff hash, dataset/schema/prompt/config/evaluator hashes, parameters/seed/repeats and safe errors. Run every example; final status aggregates error/incomplete distinctly. Do not compare across changed datasets/evaluator policies without an explicitly reviewed common rerun.
- [x] **Demonstrate green:** same pytest command exits 0; run `uv run python -m app.evals.run --agent foundation --dataset evals/foundation/1.0.0 --split dev --output data/evals` (repeat for `holdout`, `prod_regression`). Expected exit 0 and explicit synthetic complete summaries with bundle hashes; intentionally invalid candidate exits 1, execution/incomplete errors exit 2. Run `uv run python -m app.evals.compare --agent foundation --candidate <bundle-path> --baseline <fixture-baseline-path>`; exact compatible fixture comparison succeeds. Angle brackets here denote paths printed by the prior command, not shell literal arguments.
- [x] **Review/report/commit:** `feat: execute reproducible fail-closed evaluations`; record per-split counts/results and note fixture baseline is not a human-approved production baseline.

### Task 9: Phoenix dataset/experiment adapter and context compatibility probe

**Files:** Create `app/evals/phoenix.py`, `tests/integration/test_phoenix_adapter.py`, `tests/integration/test_phoenix_live.py`. Modify `observability/README.md`, `docs/dependencies.md`, `app/evals/run.py` for optional `--phoenix`, `app/observability/tracing.py` for tested project routing.

**Interfaces:** Produce `PhoenixAdapter.register_dataset(dataset: LoadedDataset) -> DatasetRegistration`, `PhoenixAdapter.publish_experiment(bundle: ExperimentBundle) -> ExperimentRegistration`, `PhoenixAdapter.export_offline(bundle: ExperimentBundle, directory: Path) -> Path`. Registration models bind local immutable hashes, sanitized remote IDs/status and project. Use the Task 5 pinned SDK/image boundary; registration is explicit, never implied by creating a project.

- [x] **Write failing tests:** `test_register_all_examples_annotations_and_scores`, `test_idempotent_version_hash_registration`, `test_phoenix_failure_preserves_complete_local_bundle`, `test_remote_failure_not_experiment_success`, `test_registration_redacts_holdout_and_private_data`, `test_two_projects_same_trace_parent_ids`, `test_replayed_span_duplicate_behavior_documented`. Fake SDK asserts exact dataset/experiment APIs and per-example linkage. Live tests require an explicit marker/environment flag.
- [x] **Demonstrate red:** `uv run pytest tests/integration/test_phoenix_adapter.py -q`; expected missing adapter. Never use a skipped live test as evidence for the integration.
- [x] **Implement minimum:** version-pinned SDK adapter, dataset/hash idempotency mapping and explicit experiment score/annotation upload. General projects receive only sanitized rights-cleared inputs; holdout detailed inputs/labels stay local in restricted bundle and Phoenix receives opaque IDs/aggregate results unless a separately restricted project is demonstrably configured. Preserve local evidence on remote errors. Keep all eight blueprint project names; first compatibility probe uses foundation spans routed through orchestrator and news-scout project names without claiming a scout agent exists.
- [x] **Demonstrate green/offline:** same test command exits 0; with Phoenix unreachable, exported bundle includes schema/provenance, all required sanitized scores, span context and manifest hash, and can be reloaded/replayed. This tested documented fallback satisfies outage acceptance; remote registration status remains pending/failed.
- [x] **Demonstrate live when available:** `docker compose -f observability/docker-compose.yml up -d`, then `FINANCE_PHOENIX_LIVE=1 uv run pytest tests/integration/test_phoenix_live.py -m phoenix_live -q`. Record actual project/trace/dataset/experiment IDs and UI evidence. Probe same trace across two projects and replay duplicates. If UI lacks unified tree, keep projects/context and provide run-index links in Task 13; document observed limitation. Do not claim trace visibility without evidence. If Docker unavailable, record reason and use proven offline fallback rather than installing infrastructure silently.
- [x] **Review/report/commit:** `feat: integrate Phoenix experiments with offline evidence`; include compatibility observations and sanitized evidence in report.

### Task 10: Release eligibility, baseline bootstrap and feedback quarantine

**Files:** Create `app/release/{models,check,service}.py`, `app/storage/migrations/005_releases.sql`, `tests/unit/test_release_gate.py`, `tests/integration/test_release_registry.py`, `tests/integration/test_feedback.py`. Modify `app/evals/compare.py` to resolve `--baseline approved` from registry.

**Interfaces:** Produce `ReleasePolicy`, `ReleaseCandidate`, `GateDecision`, `ReleaseApproval`, `ReleaseService.check(candidate: ReleaseCandidate) -> GateDecision`, `ReleaseService.approve(candidate_hash: str, principal: Principal, expected_version: int, bootstrap: bool, idempotency_key: str) -> ReleaseApproval`, `ReleaseService.active(agent_name: str) -> ReleaseApproval | None`, `queue_feedback(annotation: FeedbackAnnotation) -> FeedbackCandidate`. Feedback types store local annotation refs, rights/privacy/adjudication review states and affected agent/dataset; never modify a released dataset. `approve` is not wired to CLI/worker and must be invoked only by Task 12 authenticated route.

- [x] **Write failing tests:** `test_no_baseline_requires_human_bootstrap`, `test_eligibility_not_approval`, `test_missing_split_evaluator_or_calibration_blocks`, `test_critical_false_accept_blocks`, `test_changed_bundle_or_configuration_revokes_eligibility`, `test_incomparable_baseline_blocks`, `test_complete_offline_export_allowed_missing_evals_denied`, `test_synthetic_release_cannot_enable_real_agent`, `test_release_lost_response_retry_returns_original_one_event`, `test_release_key_changed_payload_conflicts`, `test_revoked_release_retry_does_not_reactivate`, `test_feedback_quarantined_not_dataset_mutation`. Assert agent principal cannot approve, approved baseline is immutable, judge-only success cannot promote, and all thresholds apply per split/required repeat rather than only pooled means.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_release_gate.py tests/integration/test_release_registry.py tests/integration/test_feedback.py -q`; expected missing release registry.
- [x] **Implement minimum:** separate eligibility from authenticated promotion. Bootstrap compares against no baseline only after all intrinsic gates pass, sets `REQUIRES_BOOTSTRAP_APPROVAL`, and awaits human action. Subsequent versions compare pinned approved baseline and candidate on identical dataset/evaluators. Any critical regression blocks unless corrected/re-evaluated; do not permit a numeric override of critical policy. Dataset-type separation confines synthetic approvals to foundation demo. Restrict production runner to exact approved versions/configurations and live deterministic validators; no evaluation-degraded mode.
- [x] **Pin approval replay semantics:** require a current authenticated allowed session before any replay lookup. Scope the key to principal provider/numeric ID and approval operation; fingerprint candidate/config/experiment hashes, expected version and bootstrap flag. Identical retries return the original immutable receipt without another approval, baseline update or audit event; changed-payload reuse returns conflict. Check an existing identical receipt before applying current-version mutation checks so lost-response retries succeed. Replaying a subsequently revoked receipt never reactivates it; return its historical receipt and show current status separately.
- [x] **Demonstrate green:** same tests exit 0. `uv run python -m app.release.check --agent foundation --candidate <candidate-manifest-path>` prints machine-readable readiness; exit 0 means already human-approved exact release, exit 1 means blocked or awaiting bootstrap approval, exit 2 means evaluation/config error. Before Task 12 there is no public approval command. Milestone C report includes all three split bundles, comparison/gate outcome, Phoenix IDs or tested offline export, and no real-agent quality claim.
- [x] **Review/report/commit:** `feat: enforce human-approved release governance`.

### Task 11: GitHub OAuth and server-side reviewer sessions

**Files:** Create `app/auth/{github,sessions,dependencies}.py`, `app/web.py`, `app/storage/migrations/006_auth.sql`, `tests/unit/test_github_auth.py`, `tests/integration/test_sessions.py`. Modify `pyproject.toml`, `uv.lock`, `.env.example`, `docs/dependencies.md`, `docs/foundations-runbook.md`.

**Interfaces:** Produce `GitHubIdentity` (numeric ID, account type, display login), `GitHubOAuth.begin(browser_binding: str) -> AuthorizationRequest`, `GitHubOAuth.complete(code: str, state: str, browser_binding: str) -> GitHubIdentity`, `SessionService.create(identity: GitHubIdentity) -> SessionCookie`, `SessionService.require(session_cookie: str, now: datetime) -> Principal`, `SessionService.logout(session_cookie: str) -> None`, `require_reviewer(request: Request) -> Principal`. `create_app(settings: Settings, services: Services) -> FastAPI` wires injected storage/auth/clock/HTTP transport, never creates credentials automatically. SessionCookie contains opaque random value for HttpOnly browser use; DB stores only its hash, allowed ID, creation/last-seen/expiry and revocation. Credentials/code/token/CSRF values never enter repr/log/telemetry.

- [x] **Write failing tests:** `test_oauth_state_browser_binding_pkce_single_use`, `test_callback_expired_replayed_or_wrong_host_denied`, `test_account_numeric_id_not_rename`, `test_wrong_id_or_bot_denied`, `test_no_first_login_auto_enrollment`, `test_session_fixation_rotation_logout`, `test_idle_1800_absolute_28800_expiry`, `test_allowlist_change_invalidates_existing_session`, `test_forged_principal_header_and_csrf_denied`, `test_tokens_discarded_and_callback_not_traced`. Use mock GitHub token and `/user` responses, fake clock and test client; assert no request for repo/email scopes and no credentials persisted in any application table/export.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_github_auth.py tests/integration/test_sessions.py -q`; expected missing auth service. No real GitHub calls or secrets in CI.
- [x] **Implement minimum:** verify current official GitHub OAuth/API and selected OAuth client documentation; pin FastAPI/server/OAuth/HTTP test dependencies. Browser flow uses one-time random state, S256 PKCE and exact configured callback `http://127.0.0.1:8765/auth/github/callback`. Initial auth transaction expires after 5 minutes; resolve authenticated account server-side, match explicit numeric allowlist, reject non-User accounts, discard token after identity lookup. Missing allowlist/credentials disables approval with a clear local setup status; never fallback to gh/PAT/anonymous auth.
- [x] **Implement session transport:** `GET /auth/login`, `GET /auth/github/callback`, authenticated `POST /auth/logout`; random hashed server-side sessions, login rotation, idle 1,800 s/absolute 28,800 s. HttpOnly SameSite=Lax cookies and CSRF tokens with exact origin/Host checks for mutations. Only explicit HTTP loopback development mode allows non-Secure cookies; other HTTP hosts fail startup. Bind `127.0.0.1`; no CORS wildcard. Reject untrusted forward headers and suppress callback/access-query logs. Callback establishes identity only and never approves content/releases.
- [x] **Demonstrate green:** same pytest command exits 0; raw DB/log/export sentinel inspection confirms token/private identity exclusion from telemetry. Add instructions for the user to register their own OAuth app and configure numeric account ID; never read/print secret values. Manual local login smoke test is optional until credentials are provided, and must be reported as not run if unavailable.
- [x] **Review/report/commit:** `feat: authenticate human reviewers through GitHub`; document local trusted-machine threat boundary. Stored sessions do not defend against a malicious OS administrator or browser-profile access; no worker-facing authentication bypass is offered.

### Task 12: Browser-bound content and release decisions with review navigation

**Files:** Create `app/review/{service,routes}.py`, `app/ui/review.py`, `app/storage/migrations/007_review.sql`, `tests/integration/test_approval.py`, `tests/integration/test_review_ui.py`, `tests/integration/test_release_approval.py`. Modify `app/web.py`, `app/release/service.py`, `app/workflow/state.py`, `pyproject.toml`, `uv.lock`, `.env.example`, `docs/foundations-runbook.md`.

**Interfaces:** Produce `HumanDecision` (reviewer provider/ID, action/time, exact manifest hash/version, notes, trace ID), `ReviewService.decide(story_id: UUID, decision: Literal['approve','reject','revise'], manifest_hash: str, expected_version: int, idempotency_key: str, principal: Principal, notes: str) -> HumanDecision`, `ReviewService.release_decide(candidate_hash: str, expected_version: int, bootstrap: bool, principal: Principal, idempotency_key: str) -> ReleaseApproval`. Consume real live-session Principal only through Task 11 dependencies; route data never sets actor identity or trust. Human identity remains in protected local audit DB; trace event contains opaque decision ID only.

- [x] **Write failing tests:** `test_current_human_exact_manifest_can_approve`, `test_anonymous_agent_forged_or_expired_session_cannot_approve`, `test_stale_hash_or_version_conflicts`, `test_concurrent_edit_and_approve_never_stale`, `test_expired_license_or_missing_evidence_blocks`, `test_all_member_changes_revoke`, `test_factual_edit_reverifies_visual_edit_rerenders`, `test_csrf_get_request_and_cross_origin_cannot_decide`, `test_content_approval_not_release_approval`, `test_bootstrap_release_needs_distinct_human_action`, `test_release_form_retry_reuses_key_without_new_event`, `test_release_form_changed_payload_same_key_conflicts`, `test_no_publish_route_or_adapter`. Assert exactly one winning transaction/audit event, old approval inactive, current state consistent with latest manifest, and no network publication call even for approved content.
- [x] **Demonstrate red:** `uv run pytest tests/integration/test_approval.py tests/integration/test_review_ui.py tests/integration/test_release_approval.py -q`; expected absent review endpoints/service.
- [x] **Implement browser/backend integration:** Streamlit at `127.0.0.1:8501` reads local read-only summaries through a read-only repository connection. Show status, synthetic marker and links to backend `/review/{story_id}` and `/releases/{candidate_hash}`. The backend independently requires authenticated session and renders full preview, narration/script, claim/source/verification ledger, captions, charts and asset/license register. Browser top-level navigation to port 8765 carries its HttpOnly session; neither Streamlit server nor worker copies cookies/tokens or asserts reviewer ID. The backend serves hash-resolved media only after authentication with correct media type and no arbitrary paths.
- [x] **Implement decisions:** backend HTML forms `POST /review/{story_id}/decision` and `POST /releases/{candidate_hash}/decision` carry server-generated CSRF, a stable per-form idempotency key, and expected manifest/version; approval/rejection/revision remains an explicit human action. Preserve the key through lost-response retries, pass it unchanged through both review/release service layers, and bind its fingerprint to the exact decision payload. Render source text escaped and links restricted to http(s), never execute retrieved HTML/instructions. Recheck live session/allowlist/current hashes, policy/license validity, release eval bundle and baseline inside the transaction; approval update and event are atomic. Only reject/revise actions accept non-ready content; no approval override for critical failures. Revocation events preserve history, never delete approval rows. Local direct DB tampering is outside this application's authentication guarantee.
- [x] **Demonstrate green:** same pytest command exits 0 including rendered-form/link/CSRF/race tests. Demo approval uses test-auth injection only in tests; production startup rejects test-auth configuration. With actual GitHub credentials manually verify sign-in → review → approve → edit → approval revoked; otherwise state explicitly that live OAuth smoke remains untested, while mocked callback/service integration passed.
- [x] **Review/report/commit:** `feat: bind authenticated review to immutable artifacts`; document that approved synthetic artifacts are not publishable media and publication remains unreachable.

### Task 13: Per-agent monitoring template and local alerts

**Files:** Create `app/monitoring/{queries,ui}.py`, `tests/unit/test_monitoring.py`. Modify `app/ui/review.py`, `docs/foundations-runbook.md`.

**Interfaces:** Produce `MonitoringRepository.summary(agent_name: str, version: str | None, since: datetime) -> AgentSummary`, `MonitoringRepository.run_index(run_id: UUID) -> RunIndex`. AgentSummary reports counts/error/retry/schema/policy/eval failures, p50/p95 latency, usage availability, measured/estimated currency-separated cost, override rate and degradation. RunIndex links story traces/projects/experiments/state events with no private reviewer data.

- [x] **Write failing tests:** `test_dashboard_latency_cost_and_missing_data`, `test_run_index_links_projects_without_unified_tree`, `test_critical_failures_create_local_alert`. Seed known stage latencies and distinct currencies; assert exact counts/percentiles, no invented zero usage, stable links and missing data indicators. Critical failures cover unsupported claims reaching review, missing asset rights, telemetry failures and forbidden publishing attempts; only local alerts are produced.
- [x] **Demonstrate red:** `uv run pytest tests/unit/test_monitoring.py -q`; expected missing monitoring interface.
- [x] **Implement minimum:** common Streamlit page filtered by agent/version/Phoenix project, tested with fake-stage events. Run index uses stable run IDs and explicit project/span links when Phoenix lacks unified navigation. Keep private identity and source bodies out of summaries/links. No external alert delivery.
- [x] **Demonstrate green:** same test command exits 0; deterministic query fixtures assert alerts and expected summary values. Define percentile convention as nearest rank for nonempty windows; empty windows return null plus sample count 0.
- [x] **Review/report/commit:** `feat: expose per-agent monitoring and local alerts`; real agent dashboard validation remains part of each later agent slice.

### Task 14: Reference-aware retention and cleanup

**Files:** Create `app/maintenance.py`, `tests/integration/test_retention.py`. Modify `docs/foundations-runbook.md`.

**Interfaces:** Produce `cleanup(now: datetime, dry_run: bool = True) -> RetentionReport`; report has eligible/deleted/protected refs, reasons, byte counts and errors. Consume immutable artifact/review/baseline references and outbox delivery state from earlier tasks.

- [x] **Write failing tests:** `test_retention_30_days_preserves_active_refs_baselines_pending_outbox`, `test_dry_run_does_not_delete`, `test_retention_failure_keeps_audit_and_reports_error`. At the 30-day boundary, assert only older unreferenced completed telemetry/eval bundles eligible; active review/audit lineage, released datasets, baselines and pending queue survive. Verify before/after files and row counts identical in dry-run.
- [x] **Demonstrate red:** `uv run pytest tests/integration/test_retention.py -q`; expected missing cleanup function.
- [x] **Implement minimum:** dry-run default; explicit execute flag for actual cleanup. Never cascade-delete live lineage or immutable dataset versions. Recheck references transactionally before deletion and report partial failure rather than deleting DB evidence for files still present. No scheduled cleanup task is created.
- [x] **Demonstrate green:** same test command and `uv run python -m app.maintenance --dry-run` exit 0 with deterministic retention report and no mutation.
- [x] **Review/report/commit:** `feat: protect active lineage during local retention`.

### Task 15: End-to-end synthetic acceptance, CI and operational docs

**Files:** Create `app/demo.py`, `tests/integration/test_foundation_e2e.py`, `.github/workflows/ci.yml`. Modify `docs/foundations-runbook.md`, `observability/README.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/EVALUATION_PLAYBOOK.md`, `TASKS.md` in relevant sections to align tested interfaces.

**Interfaces:** Produce `run_demo(output_dir: Path, telemetry_mode: Literal['offline','phoenix']) -> DemoReport`. DemoReport explicitly says `synthetic_foundation`, binds all hashes/versions/outcomes and links local artifacts/traces/experiment summaries/current review manifest. Consume existing service interfaces; no new model/agent abstraction.

- [x] **Write failing tests:** `test_full_fake_stage_eval_review_rework`, `test_phoenix_outage_no_silent_loss`, `test_model_timeout_partial_output_and_disk_fault`, `test_e2e_secrets_and_article_body_absent`, `test_no_auto_publish_even_after_approval`. Assert current artifact lineage, all required eval bundles, expected critical negative rejections and no unsupported downstream advance; telemetry failures are surfaced. Inject authenticated sessions only through test-client dependencies.
- [x] **Demonstrate red:** `uv run pytest tests/integration/test_foundation_e2e.py -q`; expected missing integrated demo/report.
- [x] **Implement acceptance demo and CI:** `uv run python -m app.demo --offline --output data/foundation-demo` creates synthetic artifacts, runner attempts, sanitized trace export, all split experiment bundles and negative gate cases. It ends at `AWAITING_APPROVAL`, never signs in or auto-approves. E2E tests inject authenticated test-client sessions to exercise human-action boundaries; the CLI cannot. CI uses locked runtime/dependencies and offline tests only, no OAuth secrets, Docker, external feeds or model APIs. Use least-privilege CI token with no publication permission and record its offline report as artifact.
- [x] **Demonstrate green:** `uv sync --locked`, `uv run pytest tests/unit tests/integration -m 'not phoenix_live' -q`, the offline demo command, and `uv run python -m app.maintenance --dry-run` exit 0. Full demo report includes failed negative cases as expected rejections, no unsupported downstream advancement, complete synthetic evals, current review manifest and telemetry fallback evidence. Run Task 9 live check only when available; do not label skipped live tests as passing. Optional browser smoke evidence excludes tokens/account details.
- [x] **Review/report/commit:** `feat: validate monitored foundation workflow end to end`; Milestone D report includes files, exact commands/results, dataset/experiment versions, all gate outcomes, trace IDs or tested offline bundle, manual OAuth/real-Phoenix gaps, and a clear no-agent/no-publication statement. All critical spec and quality review findings must be resolved before declaring foundations complete.

## Future agent slices: ordered handoff, not current implementation

After Milestone D is accepted, create a separate detailed plan for **News Scout only**. Define audience/geography, sources/fetch limits/freshness and duplicate calibration before implementation. Add the scout input/output schemas, immutable prompt manifest, source adapter fixtures, deterministic normalizer/dedupe, typed runner integration, rights-cleared adjudicated dev/holdout/regression sets, real calibrated rubric, versioned thresholds, its Phoenix project/dashboard, approved baseline and a restricted local shadow run. Do not enable it based on foundation synthetic results.

Subsequent slices remain ordered: Novelty Analyst → Researcher → Fact Checker → Scriptwriter → Producer. Growth Analyst follows separately authorized metrics availability. Each gets the full per-agent contract/golden/rubric/trace/dashboard/release process before enablement. Preserve the novelty prompt's deterministic weighted score and 70/100 threshold; never use it to compensate for failed credibility. Script duration is 60–75 seconds; chart values, claim mappings, caveats, and asset rights remain hard checks. No model/hardware/TTS/render/platform choice is implicitly approved by this roadmap.

Real source outage, partial render and end-to-end editorial quality chaos tests become agent-slice requirements when those adapters exist. Foundation fakes test timeout/storage/retry plumbing only, not actual retrieval/rendering behavior. Real publishing requires a new design for durable request outbox, receipt reconciliation, exact active approval/rights/kill-switch validation, and separate explicit authorization.

## Acceptance matrix and plan self-review

These assertion anchors supplement the named tests and negative cases in each task. Construct fixtures in that task's named test file; variables below denote that test's returned value, never unimplemented production helpers.

| Task / named test | Minimum assertion in code |
| --- | --- |
| 1 / `test_unknown_usage_is_null_not_zero` | `assert usage.input_tokens is None` and `assert usage.measured_cost is None` |
| 2 / `test_missing_material_verdict_blocks` | `assert report.ready is False` |
| 3 / `test_same_bytes_same_ref` | `assert store.put(b"fixture", "text/plain", "1.0") == store.put(b"fixture", "text/plain", "1.0")` |
| 4 / `test_edit_revokes_approval_atomically` | `assert snapshot.state != StoryState.APPROVED_FOR_PUBLISH` and `assert active_approval is None` |
| 4 / `test_early_evidence_gap_corrects_and_resumes` | For each checkpoint in `(StoryState.DISCOVERED, StoryState.CLUSTERED)`: `assert resumed.state == checkpoint` after corrected-input validation; only normal subsequent transitions can reach `SCORED` |
| 4 / `test_create_retry_same_request_one_event_changed_payload_conflicts` | `assert replay == original` and `assert len(events) == 1`; changed candidate hash under the same scoped key must raise a conflict |
| 5 / `test_overflow_preserves_queue_marks_run_degraded` | `assert pending_after == pending_before == 10000` and `assert run.observability_status == "DEGRADED"` |
| 6 / `test_timeout_three_attempts_stable_run_distinct_invocations` | `assert len(attempts) == 3` and `assert len({a.invocation_id for a in attempts}) == 3` |
| 7 / `test_holdout_tune_denied` | `with pytest.raises(ValueError, match="holdout"):` around `load_dataset(root, "holdout", "tune")` |
| 8 / `test_skipped_required_check_incomplete` | `assert bundle.status == "INCOMPLETE"` |
| 9 / `test_phoenix_failure_preserves_complete_local_bundle` | `assert reloaded_bundle == original_bundle` after export/reload despite remote failure |
| 10 / `test_no_baseline_requires_human_bootstrap` | `assert decision.status == "REQUIRES_BOOTSTRAP_APPROVAL"` and `assert releases.active("foundation") is None` |
| 10 / `test_release_lost_response_retry_returns_original_one_event` | `assert replay == original_receipt` and `assert len(approval_events) == 1` after retrying the exact authenticated request |
| 10 / `test_revoked_release_retry_does_not_reactivate` | `assert replay == original_receipt` and `assert releases.active("foundation") is None` after revocation and identical retry |
| 11 / `test_forged_principal_header_and_csrf_denied` | `assert response.status_code == 401` for a request with no valid session despite supplied identity headers |
| 12 / `test_stale_hash_or_version_conflicts` | `assert response.status_code == 409` with a valid reviewer session but stale manifest |
| 12 / `test_release_form_retry_reuses_key_without_new_event` | `assert retry_payload["idempotency_key"] == original_payload["idempotency_key"]` and `assert len(approval_events) == 1` |
| 13 / `test_dashboard_latency_cost_and_missing_data` | `assert summary.sample_count == 0` and `assert summary.p95_latency_ms is None` for an empty time window |
| 14 / `test_dry_run_does_not_delete` | `assert before_files == after_files` and `assert before_rows == after_rows` |
| 15 / `test_no_auto_publish_even_after_approval` | `assert publisher_calls == []` and `assert snapshot.state != StoryState.PUBLISHED` |

| Approved design requirement | Owning task and proof |
| --- | --- |
| Strict versioned envelopes, prompts/schemas, numeric/date distinctions | 1/2: validation and snapshot tests |
| Evidence revisions, missing dates, caveats, unsupported facts | 2/4: evidence and readiness negatives |
| Immutable artifacts, migrations, crash/race/rework audit | 3/4: disk faults, two DB connections, full state matrix |
| Runner/provider boundary, retry metadata, interrupted attempts | 6: timeout/schema/crash tests, no remote provider |
| All trace metadata, sanitization, outbox bounds/replay | 5/6: raw byte inspection, context and overflow checks |
| Per-agent Phoenix projects, datasets, experiments and views | 9/13: pinned compatibility probe, registration tests, run index/fallback |
| Locked event-level splits, rights/adjudication, illustrative exclusion | 7: leakage/hash/gate-denial tests |
| Deterministic and calibrated rubric interfaces, failure semantics | 8/10: fake judge errors, missing calibration gate denial |
| Reproducible bundles, baseline comparison, human release bootstrap | 8/10/12: hash/provenance/gate and authenticated release tests |
| GitHub account allowlist, sessions and human-only approval | 11/12: mocked auth/CSRF plus optional real login smoke |
| Edit-after-approval invalidation, rights, no publishing | 4/12/15: concurrent edit, all manifest members, forbidden publication |
| Feedback privacy/licensing and no released-set mutation | 10: quarantined annotation states |
| Monitoring/alerts, retention and offline operation | 13/14/15: summary/retention/e2e tests, synthetic offline report |
| Per-agent datasets/evals/dashboards and model benchmarks | Future agent plans, explicitly not satisfied by foundations |

Self-review must check this table against the approved spec, every Interfaces block against its callers, every step for one checkable outcome, and all five Review Focus items against their owning tests. No unspecified future interface may be required for a foundation task to pass. Resolve any discovered gaps in the plan before execution; do not silently substitute placeholders or skipped checks.

Planning self-review completed on 2026-11-09: mapped all approved foundation requirements to tasks; checked producer/consumer names and five Review Focus cases; made edit invalidation conservative and hash-derived; kept synthetic/illustrative data distinct from agent evidence; and separated live Phoenix/OAuth smoke checks from default offline tests. Dependency compatibility, real OAuth credentials, live trace visibility and agent quality are execution-time evidence gaps, not reported successes. All implementation checkboxes intentionally remain unchecked.

## Handoff

Independent `gpt-6-astra` review at medium reasoning completed on 2026-11-09.
Two P2 recovery/idempotency findings were corrected and re-reviewed; the reviewer
found no remaining important blocker and marked the plan ready for Task 1.
See the [review record](../reviews/2026-11-09-astra-foundations-review.md).
No implementation or service startup occurred during review.

The user's approval covers the foundation design and creation of this detailed plan. This planning request does not implement the agents. For a later implementation request, preserve the small-slice/review gates and begin at Task 1, not the roadmap. Native execution with independent spec/quality review is the lower-context option for these sequential tasks; independent stable tasks may use the subagent workflow if chosen. No execution method or real service spend is assumed here.
