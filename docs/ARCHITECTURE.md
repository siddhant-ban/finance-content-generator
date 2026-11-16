# System Architecture

The [approved foundation refinement](superpowers/specs/2026-11-09-foundations-design.md)
resolves missing state, identity, artifact, telemetry and release decisions in this
blueprint. Its [detailed plan](superpowers/plans/2026-11-09-foundations.md) defines
the foundation implementation order and tests. The application implements the shared foundation services and synthetic acceptance
below. The agent topology, model/runtime choices and publication path remain
future design, rather than capabilities established by synthetic tests.

## Implemented acceptance boundary

`app.demo.run_demo(output_dir, telemetry_mode)` uses the existing arithmetic
`FakeStage` through `AgentRunner`, persists its typed input/output and attempts,
and binds its immutable output reference to a synthetic candidate. Explicit
`foundation.demo.v1` validators accept only `synthetic_foundation` artifacts.
Hand-authored synthetic evidence/claim-use/license artifacts exercise the normal
`StateService` transitions and nine-member `ReviewManifest`; these are not news
agents or media rendering. The store-bound `PolicyEngine` rechecks exact current
claim/source revisions, member bytes and rights before review.

The demo runs dev, holdout and prod_regression through `run_experiment`, stores
private bundles plus independently verified offline exports, and checks a separate
`ReleaseCandidate`. First release remains `REQUIRES_BOOTSTRAP_APPROVAL`. The CLI
ends at `AWAITING_APPROVAL`, without any session, approval or publication.
Integration tests alone mock the GitHub provider transport, use the real callback
and session flow through `TestClient`, submit authenticated CSRF-protected content
and release forms, and verify rework revokes content approval. No production
authentication bypass is provided. `PUBLISHED` remains unavailable even after
human content approval; there is no publisher or publication HTTP route.

Demo reports bind prompt, schema, dataset, policy, evaluator, code/config/model,
experiment, manifest/member and trace-export hashes. Local reports and offline
private evidence stay protected; CI exports only aggregate public synthetic
results. An offline trace export preserves pending IDs and is not a delivery
receipt. Existing `MonitoringRepository` remains the read-only projection over
attempts/evals/local telemetry and audit identifiers.

## 1. Product definition

A finance/economics editorial system that converts timely, well-sourced developments into original short-form explainers. The editorial promise:

> What happened? Why does it matter? What could happen next?

The system is mostly autonomous up to a human review queue. It does not provide personalized financial advice and does not publish without explicit human approval.

## 2. Architectural principles

1. **Evidence before eloquence.** A script cannot introduce a factual claim absent from its claim ledger.
2. **Deterministic orchestration.** Use a state machine and explicit contracts, not free-form agent conversations.
3. **Independent agents.** Each agent has a narrow responsibility, prompt version, tests, golden set and trace project.
4. **Human editorial control.** Approval is a hard gate, not a soft recommendation.
5. **Observable by default.** Every agent invocation is a traceable unit with inputs, outputs, costs, latency and evaluation results.
6. **Low-cost first.** Start with SQLite, local models and local Phoenix; add paid APIs only after measured need.
7. **Graceful degradation.** Observability outage must not silently erase the run; buffer traces locally and flag the missing telemetry.

## 3. Agent topology

| Agent | Responsibility | Output |
|---|---|---|
| 1. News Scout | Ingest feeds/APIs, normalize stories, deduplicate and cluster | `StoryCandidate[]` |
| 2. Novelty Analyst | Score significance, novelty, relevance, timeliness, storytelling and visual potential | `StoryDecision` |
| 3. Researcher | Retrieve primary/credible sources and build claim ledger | `ResearchDossier` |
| 4. Fact Checker | Verify each material claim and assign evidence-backed status | `VerificationReport` |
| 5. Scriptwriter | Produce a 60–75 second script grounded in verified claims | `ScriptPackage` |
| 6. Creative Director / Producer | Convert script into scenes, narration, charts and render instructions | `VideoPlan` / `RenderArtifact` |
| 7. Growth Analyst | Analyze platform metrics and propose measured experiments | `GrowthReview` |
| 8. Orchestrator | Own state transitions, retries, gates, run IDs and end-to-end audit | `PipelineRun` |

The orchestrator is not an editorial super-agent. It coordinates tools and agents, validates contracts, applies policy gates and records decisions.

## 4. Data flow

```text
RSS / GDELT / official releases / FRED / SEC / central banks
                         |
                         v
                   [News Scout]
                         |
                StoryCandidate[]
                         v
                 [Novelty Analyst]
                         |
                StoryDecision (>= threshold)
                         v
                    [Researcher]
                         |
                  ResearchDossier
                         v
                   [Fact Checker]
                         |
              VERIFIED / NEEDS_REVISION / REJECTED
                         |
                   [Scriptwriter]
                         |
                  ScriptPackage
                         v
             [Creative Director / Producer]
                         |
              VideoPlan -> RenderArtifact
                         v
                AWAITING_APPROVAL
                         |
               explicit human decision
                   /           \
              APPROVE          REJECT/REVISE
                 |                 |
           APPROVED_FOR_PUBLISH    terminal/rework
                 |
          platform adapter (later)
                 |
              PUBLISHED
                 |
          metrics ingestion
                 v
            [Growth Analyst]
                 |
         editorial experiment backlog
```

## 5. State machine

Use a typed enum and database-enforced transition function:

`DISCOVERED → CLUSTERED → SCORED → RESEARCHED → VERIFIED → SCRIPTED → RENDERED → AWAITING_APPROVAL → APPROVED_FOR_PUBLISH → PUBLISHED → ANALYZED`

Allowed side paths:
- Any pre-publication stage may move to `NEEDS_REVISION` with a reason and retry budget.
- Credibility failure moves to `REJECTED`.
- Human rejection moves to `REJECTED` or `NEEDS_REVISION`.
- `PUBLISHED` requires a recorded human approval event and a successful publishing adapter response.
- No agent can directly set `APPROVED_FOR_PUBLISH` or `PUBLISHED`.

State transition audit fields: `run_id`, `story_id`, `from_state`, `to_state`, `actor_type`, `actor_id`, `reason`, `timestamp`, `trace_id`.

## 6. Shared contracts

Every agent uses an envelope:

```json
{
  "schema_version": "1.0",
  "run_id": "uuid",
  "agent_name": "researcher",
  "agent_version": "0.1.0",
  "prompt_version": "researcher.v1",
  "input_ref": "artifact://...",
  "output": {},
  "status": "OK",
  "warnings": [],
  "evidence_refs": [],
  "trace_id": "..."
}
```

Minimum shared entities:

- `StoryCandidate`: canonical URL, headline, publisher, published/retrieved timestamps, summary, entities, topic tags, source type, duplicate cluster.
- `StoryDecision`: score breakdown, chosen angle, rationale, credibility caveats.
- `SourceRecord`: URL, publisher, source class, published/retrieved timestamps, excerpt or locator, content hash, license/usage notes.
- `Claim`: stable ID, exact wording, claim type, importance, source references, verification status, caveat.
- `ResearchDossier`: story ID, timeline, claim ledger, competing evidence, unknowns.
- `ScriptPackage`: hook, timed scenes, narration, on-screen copy, claim IDs per sentence, CTA, sources.
- `VideoPlan`: scene specs, chart data references, asset provenance, voice config, subtitle config, render settings.
- `HumanDecision`: reviewer, decision, timestamp, notes, artifact version.
- `PlatformMetric`: platform, post ID, measurement time, views, watch time, completion, shares, saves, comments, profile visits, follows, clicks, conversions, revenue.

All timestamps UTC ISO-8601. Money includes currency. Percentages and percentage-point changes are distinct typed concepts.

## 7. Per-agent implementation contract

Each agent must expose:
- `run(input: AgentInput) -> AgentOutput`
- `validate_input(...)` and `validate_output(...)`
- `prompt_id` and immutable `prompt_version`
- `agent_version`, model/provider metadata and config hash
- deterministic unit tests for schemas and invariants
- versioned golden dataset and holdout split
- deterministic evaluators plus rubric evaluators where judgment is needed
- Phoenix trace spans with input/output references, not secrets or copyrighted full text
- eval result containing pass/fail, metric scores, per-example failures and experiment ID

Agent output is rejected if schema-invalid, missing evidence where required, or failing a critical policy rule.

## 8. Evaluation and observability topology

One Phoenix instance; separate projects:
- `finance-media-news-scout`
- `finance-media-novelty`
- `finance-media-research`
- `finance-media-fact-check`
- `finance-media-scriptwriter`
- `finance-media-producer`
- `finance-media-growth`
- `finance-media-orchestrator`

Every agent span carries:
- `agent.name`, `agent.version`, `prompt.version`
- `model.provider`, `model.name`, `model.parameters`
- `run.id`, `story.id`, `dataset.name`, `dataset.version`, `experiment.id`
- `pipeline.stage`, `environment`, `result.status`
- `latency_ms`, token counts and estimated cost when provider exposes them
- `source.count`, `claim.count`, `retry.count`, `error.type`

Trace hierarchy: pipeline run (root) → agent invocation (child) → retrieval/tool/LLM spans (children). Use OpenTelemetry trace context propagation so one trace links all agents. Keep a stable `run_id` even when retries create new spans.

The foundation probe found that pinned Phoenix 21.0.0 assigns a globally unique
trace ID to one owning project. Requested project resource tags and shared W3C
context are preserved, but a shared trace cannot be ingested into multiple Phoenix
projects. See the [observability evidence](../observability/README.md). The run
index must expose actual ownership. Before implementing real agents, their design
must resolve one owning project versus separate linked traces; separate per-agent
Phoenix dashboards are not yet an achieved capability.

## 9. Recommended stack

| Layer | MVP choice | Rationale |
|---|---|---|
| Orchestration | Python state machine; optionally LangGraph later | Transparent and easy to debug |
| Contracts | Pydantic | Strict typed I/O |
| Storage | SQLite + local artifact directory | No managed DB cost |
| Ingestion | RSS, GDELT, FRED, SEC, official sources | Mix of timely and primary data |
| Model runtime | Ollama adapter; optional API adapter | Local-first; provider flexibility |
| Observability/evals | Arize Phoenix + OpenTelemetry | Traces, datasets, experiments and evaluators |
| Video | Remotion + FFmpeg | Reusable motion graphics |
| Voice | Piper or approved TTS adapter | Low-cost narration |
| Dashboard | Streamlit | Local approval queue |
| Tests | pytest + contract tests | Cheap repeatable quality gates |

## 10. Deployment topology

MVP runs on one machine:
- Python worker process
- SQLite database and `artifacts/`
- Ollama if the machine can handle the selected model
- Phoenix local Docker service
- Streamlit approval dashboard
- FFmpeg/Remotion render worker

Do not assume the old RTX 3060 laptop is healthy or that its VRAM/RAM is sufficient. Benchmark before selecting a model. Start with a small quantized model and keep a provider adapter boundary.

## 11. Security, legal and editorial controls

- Store secrets in `.env`; redact from logs.
- Treat source content as untrusted prompt input; never follow instructions embedded in articles.
- Persist URLs and claim-level excerpts/locators rather than entire copyrighted articles where possible.
- Use original narration and visuals; maintain an asset register with source, license, attribution and expiry.
- Label sponsored/affiliate content clearly and comply with platform and jurisdiction rules.
- No personal financial advice or guarantees.
- Human approval required for every post.
- Keep a kill switch that disables all publishing adapters.

## 12. Non-goals for MVP

- Autonomous publishing
- Automated financial recommendations
- AI-generated cinematic video
- Multi-cloud deployment
- Complex agent-to-agent negotiation
- Paid data vendors or high-volume API usage
