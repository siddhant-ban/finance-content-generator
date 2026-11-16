# Evaluation-first pipeline foundations design

Status: design approved 2026-11-09; foundation execution authorized 2026-11-10. Individual agents and deployment remain excluded.
Date: 2026-11-09.

## Intent and scope

Build a low-cost finance/economics editorial pipeline whose outputs are evidenced,
evaluated, observable, and held for authenticated human review. Prove the shared
infrastructure before enabling any individual agent. This design refines
`docs/ARCHITECTURE.md`, `docs/EVALUATION_PLAYBOOK.md`, and
`superpowers/IMPLEMENTATION_PLAN.md`; it does not describe an existing application.

The repository currently contains documentation, prompts, illustrative JSONL, and
a Phoenix compose starter. There is no application package, dependency manifest,
test suite, or locked runtime. The observed `python3` is 3.15.2; this is not a
compatibility decision. Existing uncommitted files belong to the user and must be
preserved.

### Confirmed requirements

- Python 3.11+; Pydantic contracts; SQLite and local artifacts for the MVP.
- Evaluation-first delivery, local-first provider abstraction, Phoenix plus
  OpenTelemetry, and a tested offline telemetry path.
- No personalized investment advice, fabricated facts, or engagement override of
  credibility. Separate facts, interpretation, scenarios, and uncertainty.
- Human approval is mandatory for each publication. Only an authenticated human
  action can enter `APPROVED_FOR_PUBLISH`.
- Browser sign-in through GitHub; only the user's account may approve.
- The initial request covered design/planning; the user subsequently approved foundation execution on 2026-11-10. Do not implement individual agents or deploy.
- No cloud deployment, paid service, social publishing or recurring spend without
  explicit user approval. The README's USD/CAD 50 target is not an approved budget.

All detailed defaults below are recommendations by Codex, subject to design
review, rather than choices already made by the user.

### Goals, non-goals, and failure conditions

The first executable milestone is a synthetic, deterministic workflow exercising
contracts, artifacts, state transitions, traces, offline replay, and release gates.
It must work without a model, network source, renderer, or publishing integration.
The subsequent milestone adds GitHub-authenticated review of a synthetic artifact.
Individual agents are later, separately reviewed slices.

Do not build all future output contracts exhaustively, real rendering, cloud
infrastructure, social adapters, growth ingestion, or an LLM orchestrator in the
foundation slice. There is no automatic publication path. A passing synthetic
experiment must never be presented as an agent quality evaluation.

Failure conditions include approval bypass, stale approval surviving an edit,
unsupported claims advancing, evaluator failure becoming a pass, silently lost
telemetry, or holdout leakage.

## Approaches

For approval identity, two approaches were presented: browser GitHub OAuth with a
review dashboard, or GitHub CLI authentication with terminal approval. The user
selected browser sign-in with approval restricted to their account. The CLI
alternative reduces web session work but fits video review less well.

For foundations, use a single local Python application with explicit services,
SQLite transactions, and dependency injection. A distributed workflow engine and
separate services add operational cost before throughput requires them. Writing
every agent contract and agent first would delay evaluation and amplify schema
rework. The recommended implementation uses narrow working slices and a fake
stage to exercise the common runner before News Scout.

## Architecture review and decisions

| Gap in blueprint | Proposed resolution | Acceptance evidence |
| --- | --- | --- |
| `NEEDS_RESEARCH` required but missing from enum | Add it as a nonterminal blocked editorial state | Transition matrix tests |
| Revision has no destination | Persist invalidated stage and explicit resume target | Rework and stale-artifact tests |
| Actor strings can impersonate a human | Auth layer creates server-side principal; approval service checks account allowlist | Forged identity denied |
| Approval artifact scope unclear | Bind to immutable review manifest covering all publication inputs | Any member change revokes approval |
| DB enforcement, races, and retries unspecified | Single transactional write service, constraints/triggers, optimistic version, idempotency key | Concurrent writer and restart tests |
| Telemetry queue only described as bounded | Durable sanitized outbox, byte/record caps, replay and explicit degradation | Outage, overflow, recovery tests |
| Phoenix projects vs trace tree unproven | Compatibility test before pinning SDK/image; cross-project navigation is not assumed | Local Phoenix trace evidence or documented fallback |
| Missing rubric service behavior | `ERROR`/`INCOMPLETE` blocks promotion; no zero-score substitution | Release gate negative cases |
| Starter JSONL not ground truth | Illustrative-only manifest; exclude from promotion | Dataset loader rejects gate usage |
| Runtime/version ambiguity | Test a supported Python 3.11+ runtime, pin lock and image by digest during implementation | Clean environment install and smoke test |

### 1. Shared contracts and evidence lineage

Use strict, versioned Pydantic models with forbidden extra fields. Version JSON
schemas and immutable prompt manifests; hash prompt contents and relevant config.
Reject unsupported major schema versions. Avoid raw model/provider payloads in
shared contracts. Define core types first: identifiers, UTC timestamps, decimal
money/currency, distinct percent and percentage-point quantities, source records,
claims, artifact references, run envelopes, state events, and review manifests.
Add agent-specific contracts only with the corresponding agent slice.

Every invocation has typed input and output envelopes with schema version, run
and story IDs, invocation/attempt ID, agent and prompt versions/content hashes,
input/output artifact references, evidence references, status, warnings, and trace
context. Error output is typed and cannot masquerade as successful content.
Separate editorial status, execution status, and observability status.

Source publication time may be unknown: represent null with an explicit reason;
never invent it. Retrieval time must be known UTC. Keep original and canonical
URLs, publisher, source class, publication/retrieval time, locator or permitted
short excerpt, content hash when available, and usage notes. A hash is not proof
that a source supports a claim. Unknown rights prevent retention of source text;
retain metadata/locators instead. Offline fixtures must be rights-cleared.

Claims have stable IDs plus immutable revisions. Evidence edges bind exact source
and claim revisions. Numeric observations include unit, period, geography,
revision/vintage, seasonal adjustment, nominal/real basis, and forecast/actual
classification; unknown fields stay explicit. Facts, interpretation, and scenarios
have different claim types. No material unsupported fact may advance.
Verification records cover every material claim; a caveated verification requires
the caveat in every downstream use. Later script contracts map factual sentences
to approved claim revisions, not just free-form IDs. Interpretation/scenario labels
do not exempt supporting factual assertions from verification.

### 2. Persistence, state, and recovery

SQLite is the local system of record for business state, audit events, immutable
artifact manifests, execution attempts, release approvals, and telemetry outbox.
Use migrations, foreign keys, WAL, bounded busy timeout, and explicit transactions.
Assume one machine and one worker initially, while testing competing review/write
requests. The state service is the sole application writer of state transitions.
Triggers reject updates outside the allowed transition matrix and audit mutation;
constraints/triggers do not authenticate a human or defend against a machine
administrator rewriting the database.

Normal progression remains:
`DISCOVERED → CLUSTERED → SCORED → RESEARCHED → VERIFIED → SCRIPTED → RENDERED → AWAITING_APPROVAL → APPROVED_FOR_PUBLISH → PUBLISHED → ANALYZED`.

Add `NEEDS_RESEARCH`, `NEEDS_REVISION`, and terminal `REJECTED`.
Evidence gaps go to `NEEDS_RESEARCH`; correctable content failures go to
`NEEDS_REVISION`; credibility failures go to `REJECTED`. Persist the last valid
artifact, failed stage, reason code, and explicit resume target. A `HOLD` scoring
decision remains `SCORED` with no downstream dispatch. No unrestricted jump back
into the normal sequence is allowed; readiness checks for the resume target run
again. Define every allowed edge in the implementation plan's transition table.

Every mutation checks expected state version and input hashes, writes event and
new state in one transaction, and uses a caller-scoped idempotency key. Repeat
identical requests return the original result; reuse with changed inputs fails.
Only successful validated stage results advance state. Transient infrastructure
failures leave editorial state unchanged and record execution failure separately.
Use at most three attempts total, with exponential backoff bounded at 30 seconds;
schema/policy failures require corrected input rather than automatic retries.
Recover interrupted attempts explicitly on restart; no claim of exactly-once
external execution. A future publishing slice needs its own durable outbox and
provider reconciliation design before activation.

Artifacts are immutable, SHA-256 addressed local files. Write temp file, flush,
atomic rename, then register in a DB transaction. A crash may leave an orphan
file, never a state referencing a missing committed artifact. Check existence and
hash before handoff; recovery reports missing/corrupt references. Reject path
traversal and escaping symlinks. Artifact reference resolution is an internal
service, never an arbitrary filesystem/URL opener.

Review manifests bind script, evidence/dossier, verification report, render,
subtitles, captions, charts, source attributions, and asset/license register.
Changing any member creates a new manifest and revokes approval transactionally.
A factual edit additionally invalidates verification and all dependent outputs;
a visual-only edit retains verification but requires render validation and review
again. Published history is immutable; later corrections are new review items.

### 3. GitHub approval boundary

Use an OAuth application, browser authorization-code flow, one-time random `state`,
S256 PKCE, and an exact configured loopback callback. GitHub documents support for
loopback redirects, PKCE, and authenticated `GET /user` identity resolution:
[OAuth flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps),
[user identity](https://docs.github.com/en/rest/users/users).
Verify API details again when pinning the implementation dependencies.

Do not request repository scopes for sign-in. Resolve the account server-side and
allowlist its numeric GitHub ID; login name is display-only. No ID is inferred from
the Git remote, and the first account to sign in is not automatically trusted.
Configure the approved ID explicitly before enabling approval. Registering the
OAuth app and providing local client credentials are execution prerequisites, not
actions taken during planning.

Recommend a small loopback-only FastAPI approval/auth service with server-side
sessions and a Streamlit read/review client. This adds a dependency but keeps OAuth
callbacks, identity, CSRF checks, and transactional approval enforcement out of
agents and browser-supplied actor fields. Streamlit is not itself an authority:
the authenticated browser submits approve/reject actions directly to the backend.
Do not forward a reviewer ID from the dashboard as proof of identity. The detailed
plan must spell out the browser/backend integration before implementing the UI.

Use HttpOnly, SameSite session cookies, session rotation at login, CSRF protection
and origin/host validation on state-changing requests. Bind services only to
loopback. Secure cookies are mandatory if deployed with HTTPS later; local HTTP
loopback requires an explicit development cookie policy. Recommended limits are
30 minutes idle and 8 hours absolute session lifetime. Logout invalidates session.
Keep GitHub tokens server-side only for identity resolution and discard afterward;
never put credentials, cookies, callback query strings, or reviewer identity in
Phoenix telemetry. Audit identity stays in the protected local approval database.

The approval service checks current allowed account, live session, CSRF, expected
state version, manifest hash, policy results, verification, and license validity
in the approval transaction. Bots, anonymous callers, and agents cannot create
approval events. Failed login, missing configuration, or expired session blocks
approval without fallback; the rest of the local pipeline remains usable.

`PUBLISHED` is reserved and unreachable in the MVP. A future adapter must reject
every state except `APPROVED_FOR_PUBLISH`, check the exact active manifest and
approval, recheck policies/rights, honor a default-off kill switch, and record the
platform receipt before marking published. No real publishing adapter belongs in
the current plan's foundation deliverable.

### 4. Observability and shared execution runner

A deterministic runner calls a fake stage first, later real agents through the
same typed interface. It validates input/output, records metadata and timing,
persists outputs, applies retry policy, and runs deterministic handoff checks.
Preserve run/story IDs across retries; each attempt gets a distinct invocation ID
and span. Model calls go through a provider interface; initial tests use a fake
provider. Ollama benchmarking and optional API adapters are later decisions.

Use OpenTelemetry context propagation, one trace per story run, invocation children,
and tool/model/evaluator children. Record prompt/model/provider/config versions,
parameters after sanitization, artifact/source references, status, errors, retries,
latency, tokens and cost when available. Unknown tokens/cost are null with a reason;
measured and estimated cost are distinct and currency-tagged. A pipeline may have
multiple story traces linked through business run IDs. Use full sampling locally
for foundational tests, never raw prompt/source bodies as span content.

Keep the blueprint's eight Phoenix project names. Before depending on cross-project
parent-child navigation, run a pinned-version integration probe with a shared
trace ID across two projects. If Phoenix UI cannot display a unified tree, retain
per-agent projects and propagated context, and provide a run-index view linking
all project/span IDs. Record this limitation rather than silently changing topology.

Sanitize structured telemetry through an attribute allowlist before durable enqueue
or export; sanitize exceptions too. Use a durable outbox and bounded background
export, never network blocking in state transactions. Proposed caps: 10,000 pending
span records or 50 MiB, whichever is reached first. A failed/overflow enqueue marks
the run degraded and increments a persistent lost-record counter; no silent drop.
Already queued records are not evicted. Do not block editorial processing solely
on Phoenix availability; never call an incompletely observed run fully healthy.
On recovery replay unacknowledged records with stable IDs; delivery is at-least-once
and consumer deduplication must be verified rather than assumed. Exporter shutdown
is bounded at 5 seconds. Outbox failure is surfaced in the local run report.

Phoenix stores experiment views and annotations; repository datasets and local
immutable experiment bundles permit reproducible evaluation during outages.
Retain telemetry/eval bundles 30 days by default; keep live artifact/review/audit
lineage until explicit deletion. Automatic cleanup never deletes records required
by an active review, baseline, or experiment. Retention is local-only in this MVP.

### 5. Evaluation framework and release governance

Implement versioned dataset manifests and JSONL validation before any real agent.
Each example has agent/dataset version, stable ID, event group, split, typed input,
reference/labels, severity, source rights, and adjudication metadata. Released
versions are content-hashed and immutable. Event groups and duplicate evidence
fingerprints cannot cross splits. Dev and `prod_regression` support tuning;
holdout examples are locked and never used for prompt/judge tuning. Routine reports
must not expose holdout examples; critical failures are adjudicated by the human
and any development transfer requires a new untouched holdout version.

The existing three starter examples are illustrative only, have no holdout, and
include unresolved references; they are not a baseline or a promotable dataset.
Use a separate clearly synthetic foundation dataset for policy/runner tests. A
real agent needs its own rights-cleared, adjudicated dev/holdout/regression sets,
rubric, and calibrated targets before any production enablement.

Evaluator results distinguish `PASS`, `FAIL`, `ERROR`, and `SKIPPED`; aggregate
incomplete experiments explicitly. Provide deterministic and rubric evaluator
interfaces; fake rubric evaluators test the harness without claiming judgment
quality. Real rubric evaluators must pin judge/prompt versions, parameters, and
human calibration. Uncalibrated or unavailable required judges block promotion.
Report denominators and sample counts alongside rates; missing samples are not
successes. Run all required examples; propagate failures even if the mean is high.

Each immutable experiment bundle records code revision and dirty diff hash when
applicable, schemas, agent/prompt/provider/model/config hashes, dataset/evaluator
versions, random seed when supported, repeat count, per-example results, aggregate
metrics, latency, usage/cost availability, errors, and trace IDs. Export machine-
readable summaries and explicit Phoenix dataset/experiment registration through a
version-pinned adapter. Data registered in Phoenix is sanitized and rights-cleared.

Promotion requires 100% schema and deterministic policy pass, zero critical false
accepts in dev/holdout/regression, all required rubric metrics meeting the agent's
versioned thresholds, and no unexplained critical regression. Initial agent
targets come from the evaluation playbook and remain provisional until calibrated.
No empty split, missing evaluator, missing baseline, illustrative data, or changed
bundle can pass. The first baseline requires explicit authenticated human bootstrap
approval; there is no implicit "no baseline means pass" behavior. Prompt/model
release approval is separate from content publication approval and binds the
exact candidate experiment/configuration hash.

Phoenix outage may use a documented complete local export for review, consistent
with the playbook. Missing evaluator results always fail closed. Default production
execution requires a previously approved release and working required validators;
degraded evaluation mode is disabled in the MVP. Human labels enter a quarantined
candidate queue after privacy, rights, and adjudication checks, never a released
dataset in place.

Provide per-agent monitoring through runner events and a common local dashboard
template filtered by agent/version/project. Include schema/policy/eval failures,
critical false accepts, retries/errors, p50/p95 latency, usage availability/cost,
human overrides, and telemetry degradation. Validate this template with the fake
stage; real agent dashboards and alerts are acceptance criteria for later slices.
Alerts are local surfaced events initially, with no email/Slack delivery.

## Delivery sequence and acceptance

1. **Contracts and persistence:** schemas, migrations, artifact store, explicit
   state matrix, approval capability boundary, audit and crash/race tests. No agent.
2. **Observable execution:** fake stage/provider, runner, sanitized durable tracing,
   project/context probe, outage/overflow/replay tests. No external model required.
3. **Evaluation and release gates:** synthetic foundation dataset, typed evaluators,
   immutable experiment bundles, baseline comparison, holdout isolation, Phoenix
   adapter and tested offline export. Synthetic success is not agent quality proof.
4. **Authenticated review:** OAuth and allowlist, manifest-bound approval/revocation,
   synthetic review UI, separate release approval, invalid session/race tests.
5. **First agent only after foundations:** News Scout source fixtures, its own
   schemas/prompt/golden data/rubric/dashboard, eval baseline, then restricted local
   pilot. Plan its implementation as a separate slice; subsequent agents each need
   the same per-agent definition of done.

Every implementation task follows demonstrated red → minimal implementation → green,
spec-compliance review, code-quality review, and a short report of files, tests,
evals, trace evidence, and gaps. Update a `TASKS.md` ledger during execution. Treat
each numbered slice as a stop/review boundary, not authorization to implement all
agents. Definition of done requires tests, relevant evals, visible Phoenix traces
or tested documented fallback, versioned schemas/prompts, and limitations docs.

## Deferred choices and implementation discretion

- Verify current primary dependency documentation and pin compatible Python,
  Pydantic, OTel, Phoenix SDK/image, FastAPI, OAuth library, and Streamlit versions
  in the plan/execution compatibility task; no speculative version pin here.
- The user must configure their numeric GitHub account ID and OAuth credentials
  during execution. This is setup, not an unresolved design choice.
- Builders may choose internal helper names, migration tooling, local ports, and
  test fixture organization within these boundaries.
- Before individual agents: decide audience/geography, source allowlist/fetch
  limits, freshness windows, near-duplicate calibration, judge calibration and
  dataset coverage per stratum. These do not block foundation planning; production
  agent enablement must wait for those agent-specific decisions.
- Benchmark available hardware before selecting an Ollama model. Do not assume
  the old GPU laptop is available or healthy.
- Before rendering: choose platform dimensions/safe areas and rights-cleared fonts,
  assets, TTS and chart policies. Before real publishing: separately approve spend,
  platform access, security controls and jurisdiction/platform requirements.

## Review record

Fresh builder review completed on 2026-11-09: "Nothing important left." The reviewer
found the foundations sufficiently specified for planning, with remaining choices
explicitly deferred or reasonably left to the implementer. The user approved this
design on 2026-11-09 after confirming browser GitHub authentication. The detailed
plan was completed, independently reviewed with GPT Astra medium, and approved
for foundation execution on 2026-11-10. Individual agent implementation remains
excluded.

At design drafting, no application tests, agent evaluations, Phoenix service,
OAuth setup, or publication had run. Current foundation execution evidence is
recorded in TASKS.md; this design document does not assert those checks passed.
