# Observability baseline

## Why Phoenix
Use Arize Phoenix as the single local observability and evaluation workspace. It supports OpenTelemetry-based tracing, datasets, experiments and evaluators. Separate projects per agent provide focused views; trace context links them into an end-to-end run.

References:
- https://arize.com/docs/phoenix/
- https://github.com/Arize-ai/phoenix

## Local start
```bash
docker compose -f observability/docker-compose.yml up -d
```
Then open `http://localhost:6006`.

Compose pins Phoenix 21.0.0 by its verified multi-platform image digest and
publishes only `127.0.0.1:6006`. UI analytics and unsolicited external resource
fetching are disabled. The image has not been started by Task 5: Task 9 tests
real ingestion and UI behavior against the pinned version.

For the explicit local liveness probe run:

```bash
uv run python -m app.observability.health
```

This probes `/healthz`, reports typed JSON, and exits 0/1. It is distinct from
proof that a particular trace reached Phoenix. Default tests never start Docker.
Full buffer/export/replay instructions are in
[the foundation runbook](../docs/foundations-runbook.md#durable-tracing-and-offline-evidence).

## Required instrumentation contract
At minimum, create spans for:
- pipeline run
- agent invocation
- each retrieval/tool call
- each model call
- each evaluator
- human approval decision
- render and publish adapter operations (publishing remains disabled until explicitly approved)

Required span attributes:
`agent.name`, `agent.version`, `prompt.version`, `model.provider`, `model.name`, `run.id`, `story.id`, `pipeline.stage`, `dataset.name`, `dataset.version`, `experiment.id`, `result.status`, `retry.count`.

Record token counts/cost only where available from the provider. Do not fabricate estimates as actual cost. Use structured events for claim IDs, source IDs and state transitions; avoid logging full article text or secrets.

## OpenTelemetry implementation notes
- Use the Python OpenTelemetry SDK and OTLP exporter.
- Set Phoenix OTLP endpoint from environment variables; do not hard-code credentials.
- Propagate W3C trace context between orchestrator and agent calls.
- Use one trace per story pipeline run; each agent invocation is a child span.
- Keep stable business identifiers (`run.id`, `story.id`) as attributes; do not use them as trace IDs.
- If Phoenix is down, use a bounded local queue/file exporter and mark the run's observability status as degraded.
- The implementation uses a dedicated SQLite outbox with transactional row/byte
  limits, same-ID replay and sticky loss reporting. Explicit offline export
  produces checksummed sanitized JSONL without draining the queue. Delivery is
  at-least-once; collector deduplication remains a Task 9 compatibility check.
- `FINANCE_PHOENIX_ENDPOINT` configures only a literal HTTP loopback endpoint
  ending in `/v1/traces`. Proxy/netrc discovery, environment credentials and HTTP
  redirects cannot override that boundary. Partial rejection preserves the batch.
- Metadata names must be runtime/release identifiers rather than retrieved text.
  The sanitizer supports the project's actual agent aliases and named prompt
  versions, numeric parameters, hash refs, UUIDs, finite measurements and bounded
  versioned model identifiers; it does not choose a model. Unknown measurements
  remain absent, never zero. Sources, input/output bodies and reviewer identity
  are not trace attributes.

## Evaluation integration
Phoenix is the system of record for experiment traces and comparison. Keep evaluator definitions and golden dataset source files version-controlled in this repository. Add an adapter layer so evaluator execution is not tightly coupled to a specific Phoenix SDK version. Export a machine-readable eval summary to CI and fail the release gate on critical regressions.

## Per-agent project names
- `finance-media-news-scout`
- `finance-media-novelty`
- `finance-media-research`
- `finance-media-fact-check`
- `finance-media-scriptwriter`
- `finance-media-producer`
- `finance-media-growth`
- `finance-media-orchestrator`

## Security
Phoenix is local by default in this starter. Do not expose it to the public internet without authentication, TLS, access controls and a retention policy. Redact credentials, personal data and copyrighted source bodies before tracing.

## Explicit foundation experiment registration (Task 9)

The default harness stays local. To explicitly upload an already recorded run:

```bash
uv run python -m app.evals.run --agent foundation --dataset evals/foundation/1.0.0 --split dev --phoenix
```

The CLI persists the private bundle and reloadable offline export before any
remote mutation. A remote failure returns exit 2, retains the local evaluation
status, and reports `phoenix.status=failed`; a local PASS does not imply remote
success. `PhoenixAdapter.publish_experiment` never calls a task or model.
The pinned client calls `datasets.create_dataset`, `experiments.create`, then
`experiments.log_run` and `experiments.log_evaluation` for each existing run and
its six evaluator results. Run times come from recorded attempts, never invented
upload times. Missing attempt times prevent detailed registration. Scores retain
PASS/FAIL/ERROR/SKIPPED labels, denominator, severity and evaluator version hashes.

Dataset and experiment resources are global in Phoenix's API. Registration's
`project` is explicit metadata; it is not an access-control boundary. Trace
routing uses the `openinference.project.name` resource attribute and preserves
W3C parent IDs across the eight project names. Foundation compatibility spans
under news-scout demonstrate routing only; there is no scout implementation.

Only fixed-schema synthetic fixture inputs/numbers, enum labels and sanitized
results leave the machine. Source strings, rights/adjudication free text, local
paths and provider configuration do not. Holdout uploads contain opaque IDs and
experiment aggregate results only: no individual runs, scores, labels, outputs,
trace IDs or reference data. No restricted-project access is assumed.

A separate SQLite registry (`phoenix-registry.db`, not business migrations)
binds endpoint/project, dataset version/hash/split and immutable bundle hash to
remote IDs. Successful registration is locally idempotent across processes.
Each mutation is journaled before transmission. An interrupted/ambiguous write
remains pending locally and returns `remote_state_unknown` on retry, requiring
operator reconciliation rather than possibly duplicating a successful run.
A refused connection/connect timeout permits a subsequent explicit replay.
A new registry is a new registration authority, not proof of remote deduplication.
SDK datasets use server-assigned example IDs; their returned opaque metadata is
validated and mapped to each existing execution, not inferred from list position.

`export_offline(bundle, directory)` produces `payload.json`, a checksummed
`manifest.json`, and a mode0700 `private/` directory of mode0600 full bundle,
original dataset manifest and the selected split. Treat the entire directory as
restricted evidence; the public payload alone is sanitized. `load_offline` checks
all file hashes, the manifest hash, the immutable bundle, the dataset manifest
and selected split pins, and reconstructs the sanitized payload to ensure no
scores/context were dropped or changed. `replay_offline` uploads the validated
existing records without original dataset files or rerunning models. This is an
integrity checksum, not an authenticated release signature. Retain the registry
with the export for safe retries. Outage acceptance is covered by actual SDK
requests against an unavailable mock transport and verified replay.

Run the local-service compatibility probe only after explicitly starting Phoenix:

```bash
FINANCE_PHOENIX_LIVE=1 FINANCE_PHOENIX_EVIDENCE=/private/tmp/finance-phoenix-live-evidence.json \
  uv run pytest tests/integration/test_phoenix_live.py -m phoenix_live -q
```

The test never starts infrastructure. It queries actual project/span IDs,
registers actual datasets/experiments, and records same-ID span replay counts.
Default skipped live tests are not integration evidence. Local outbox duplicates
coalesce before sending; OTLP delivery remains at-least-once. Collector duplicate
behavior and whether the UI renders a unified cross-project tree require the
recorded live/API/UI observations in the Task 9 report. Do not infer either from
`/healthz` or a successful export acknowledgement.

Observed Phoenix21 compatibility limitation: a trace is owned by one project.
The pinned server's `Trace` model has one `project_rowid` and a global unique
constraint on `trace_id` ([source](https://github.com/Arize-ai/phoenix/blob/arize-phoenix-v21.0.0/src/phoenix/db/models.py#L747)).
Root's live diagnostic sent identical-trace parent/child spans requesting different
project resources, both in a mixed batch and separately; both were stored under
news-scout, with orchestrator absent for that trace. Splitting requests does not
solve this server storage boundary. Keep each span's intended resource project
and original context; do not fabricate trace IDs to force separate projects.
Separate standalone traces can still route to each named project. Task13 must
provide a run index with intended-project/context and owning-project trace links.

Adapter validation re-reads each `LoadedDataset` from its private loader source
root and compares the canonical wrapper and every selected row before projection
or remote registry mutation. The private source root is excluded from ordinary
repr/serialization. Copied split labels, modified rows and changed source pins
are rejected. Bundle executions must belong to the selected pinned split;
valid incomplete subsets remain supported and retain their nonpassing local
status in public evidence and experiment metadata. Public evaluator metrics
come from the fixed evaluator vocabulary; malformed diagnostic metric text stays
restricted, with its public result marked ERROR.

Offline export retries reuse an independently verified matching immutable tree.
New exports are built in a restricted sibling staging directory, reloaded and
verified completely, then atomically renamed under a POSIX publication flock.
Cooperating concurrent writers recheck the destination under the lock; complete
existing exports are never overwritten. Readers see absence or a complete tree.
The lock is released on process death. A killed process can leave a restricted
unpublished staging directory for operator cleanup; it cannot expose a partially
published export or prevent a later retry. This assumes a trusted local filesystem.

## Foundation end-to-end offline acceptance

`uv run --offline python -m app.demo --offline --output data/foundation-demo`
creates the fake runner journal, all three synthetic evaluation bundles and
independently verified offline experiment snapshots. `demo-report.json` binds
current manifest/member hashes, experiment provenance, trace IDs and the checksummed
`traces/spans.jsonl`. Pending rows and original trace/span IDs remain intact.
This evidence tests the fallback without starting a collector. The integration
suite injects export failure and checks degradation, preserved rows and matching
export identities; it also checks retries, malformed model output and storage
failure without downstream state advancement.

Explicit `--phoenix` performs loopback registration and trace export after local
evidence is persisted. The report surfaces each registration/export failure;
local evaluation PASS does not imply remote visibility. Offline snapshots remain
restricted, including the holdout private directory. CI uploads only the separate
sanitized aggregate `public-report.json`, never traces or the whole data tree.
Native FastAPI tracing remains disabled so callback/session data cannot leak.
Current live Phoenix/OAuth checks are separate manual gaps; historical Task 9
probe evidence does not establish current collector availability.
