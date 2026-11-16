# Superpowers Implementation Plan

## Detailed foundation plan — 2026-11-09

The [approved design](../docs/superpowers/specs/2026-11-09-foundations-design.md)
and [detailed foundation plan](../docs/superpowers/plans/2026-11-09-foundations.md)
refine this original roadmap. Follow their contracts → state/artifacts → tracing →
evaluation/release gates → GitHub-authenticated review sequence before implementing
News Scout. They define exact paths, interfaces, red/green checks, recovery tests,
Phoenix/offline evidence and milestone acceptance. This section does not claim
implementation or authorize agents/publication. The phases below remain the
longer-term roadmap; approval/state foundations move earlier than old Phase 5.

The [independent GPT Astra medium review](../docs/superpowers/reviews/2026-11-09-astra-foundations-review.md)
is resolved in the detailed plan. Its review-resolution table maps early-stage
`NEEDS_RESEARCH` recovery to Task 4, and create/release-approval idempotency to
Tasks 4, 10 and 12. Those tasks include lost-response retry, changed-payload
conflict, early checkpoint recovery, and revoked-approval replay tests. The
reviewer's readiness assessment is planning approval, not evidence that the
implementation tests have run.

Use the Superpowers skills in the listed order. Each task should be small enough for a fresh implementer subagent and must receive a spec-compliance review followed by a code-quality review.

## Phase 0 — Design and repository baseline
1. Run `using-superpowers`; inspect repository, Python/runtime versions and available hardware.
2. Run `brainstorming` against `docs/ARCHITECTURE.md`; record decisions and unresolved questions.
3. Run `writing-plans`; convert this plan into executable tasks with exact paths and tests.
4. Create a git worktree/branch; establish baseline tests.

## Phase 1 — Contracts and deterministic state machine
1. Implement Pydantic contracts for shared entities.
2. Implement state enum and transition service with immutable audit log.
3. Add tests proving no agent can set human approval or publication states.
4. Add artifact storage interface backed by local files + SQLite.
5. Review and merge this slice before continuing.

## Phase 2 — Observability foundation
1. Add OpenTelemetry SDK, OTLP exporter and trace context propagation.
2. Add `AgentRunner` wrapper that records agent, prompt, model, run/story IDs, status, latency, retries and token/cost metadata when available.
3. Add Phoenix health check and bounded offline telemetry fallback.
4. Add integration test with a local fake OTLP endpoint or test exporter.
5. Confirm traces appear in local Phoenix; record screenshots/trace IDs in the implementation report.

## Phase 3 — Evaluation framework before agent proliferation
1. Implement dataset schema, JSONL loader, split validation and dataset version checks.
2. Implement evaluator interface: deterministic evaluator, rubric evaluator adapter and aggregation.
3. Implement experiment runner and baseline comparison.
4. Add release gate: contract pass, critical invariants pass, no critical regression.
5. Register the starter examples as illustrative only; do not treat them as real ground truth.
6. Demonstrate the synthetic foundation experiment in Phoenix (or tested documented offline export); demonstrate a real agent's own golden-set experiment in that later agent slice.

## Phase 4 — Agents in dependency order
For each agent below, use a fresh subagent for implementation and separate spec/code reviewers. Add its golden dataset and evaluator before enabling production execution.

1. News Scout: source adapters, normalization, dedupe and candidate output.
2. Novelty Analyst: scoring in deterministic code; model only proposes dimension scores and angles.
3. Researcher: source retrieval abstraction and claim ledger.
4. Fact Checker: independent verification, numeric consistency checks and hard reject gate.
5. Scriptwriter: claim-ID constrained script generation.
6. Producer: scene schema, chart source mapping, subtitles, render and license manifest.
7. Growth Analyst: metric ingestion, normalization and review generation.

Per-agent task checklist:
- [ ] input/output contracts and schema tests
- [ ] prompt file and prompt version
- [ ] deterministic evaluator
- [ ] golden dataset with dev + holdout + prod_regression
- [ ] Phoenix project and traces
- [ ] experiment result compared with approved baseline
- [ ] human review of critical failures
- [ ] docs and runbook
- [ ] two-stage review passed

## Phase 5 — Orchestration and approval UI
1. Implement deterministic orchestrator and persisted state machine.
2. Add bounded retries and dead-letter/revision queue.
3. Implement Streamlit approval dashboard with preview, sources, claim ledger, verification, captions and asset register.
4. Add approval event bound to exact artifact hash.
5. Add tests: edit-after-approval invalidates approval; no approval means publish adapter cannot be called.
6. Keep all publishing adapters stubbed/disabled.

## Phase 6 — End-to-end eval and monitoring
1. Create end-to-end golden scenarios including successful, failed and revised stories.
2. Add trace-level checks for missing spans, orphaned claim IDs, missing source refs and state gate violations.
3. Run end-to-end experiments and compare against a pinned baseline.
4. Add dashboard/alerts for factuality, asset rights, schema failures, latency, cost and human overrides.
5. Run chaos tests for Phoenix outage, source outage, model timeout and partial render.

## Phase 7 — Controlled pilot
1. Run in shadow mode for several days; no public publishing.
2. Human-review every output and adjudicate labels.
3. Publish manually only after the human approval gate.
4. Review spend and hardware performance; keep recurring cost under the approved budget.
5. Only consider API-based publishing after separate platform-policy and security review.

## Superpowers dispatch rules
- Delegate only tasks with stable contracts and low overlap.
- Fresh implementer subagent per task; do not reuse context-heavy implementers.
- Reviewer 1 checks spec compliance; reviewer 2 checks code quality.
- Critical review findings block progression.
- Run tests/evals before marking a task complete.
- Maintain a `TASKS.md` ledger with status, branch, owner, test command, eval result and trace link.
