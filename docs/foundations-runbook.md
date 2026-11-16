# Foundation development runbook

Use the isolated codex/finance-foundations worktree. Original checkout remains
unchanged. Setup: `uv sync --locked`; tests: `uv run pytest`.
Regenerate checked-in schemas: `uv run python -m app.contracts.export --out schemas/v1`.

Configuration uses FINANCE_ environment variables; secrets stay in ignored local
.env files and must never be printed, traced or committed. To load one explicitly,
use `uv run --env-file .env <command>`. Publishing stays disabled in these
foundations. API providers are unset and require explicit opt-in even to configure.
Fake stages are allowed only in demo/test environments.

## Synthetic acceptance (Task 15)

Run from the repository root with Python 3.12.15 and uv 0.13.4:

```sh
uv sync --locked
uv run --offline pytest tests/unit tests/integration -m 'not phoenix_live' -q
uv run --offline python -m app.demo --offline --output data/foundation-demo
```

Use an absent or empty output directory; existing evidence is never overwritten.
The demo exercises the fake arithmetic runner, service state gates, original
synthetic artifacts, all three synthetic split experiments and separate release
readiness. It ends `AWAITING_APPROVAL`, never signs in, approves, or publishes.
The schema 1.0 `DemoReport` explicitly labels `synthetic_foundation`, binds all
provenance and experiment/member/manifest hashes, and links the current local
`review-manifest.json`, immutable eval/offline evidence and `traces/` export.
`foundation.demo.v1` is an explicitly synthetic validator, not editorial readiness.

`demo-report.json` and `offline/*/private/` remain local protected evidence.
`public-report.json` contains only allowlisted aggregate synthetic outcomes and
hashes; it is the only CI artifact upload. No holdout rows/answers, sessions,
reviewer notes, credentials or local evidence paths are public. Trace JSONL is
sanitized and its checksum is in the trace manifest; export does not drain pending
outbox rows or prove collector receipt. `--phoenix` explicitly enables loopback
registration/export and reports failures alongside the verified offline evidence.
A local PASS can coexist with remote failure; inspect those fields separately.

The E2E suite mocks GitHub HTTP exchange and uses real callback/session/CSRF form
requests to exercise content approval, separate release bootstrap, stale manifests
and edit revocation. It checks unavailable telemetry, timeout retries, invalid
partial output, disk faults and unavailable publication. It never uses a production
login bypass. Actual OAuth login and current Phoenix UI visibility remain manual
checks; skipped live tests are not successes. No real agents, retrieval, rendering,
paid providers, services or publication are exercised by default acceptance.

For a retention command that inspects a fresh absent root without touching existing
scratch evidence:

```sh
FINANCE_DATA_DIR=/private/tmp/finance-retention-new-root uv run --offline python -m app.maintenance --dry-run
```

Choose an absent root. Existing legacy/corrupt evidence or active nonempty WALs
can legitimately produce exit 2 with protected files. Do not remove data to make
this command pass. CI uses its own absent `data/ci-maintenance-empty` root.

## Durable tracing and offline evidence

`Telemetry(Path("data/telemetry.sqlite"))` uses a dedicated SQLite outbox, separate
from the business connection. Create the parent directory before construction.
Use `story_context(run_id, story_id)` and `span(name, project, attributes)` with
typed business UUIDs. The SDK generates trace/span IDs. Manual instrumentation
retains only the documented metadata vocabulary; names, resources, events and
exception paths are sanitized before buffering or network export.

Queue limits are 10,000 pending records or 50 MiB of serialized payload, whichever
is reached first. Lower per-instance limits are supported. Overflow preserves the
existing queue and records loss/degraded run IDs. Export uses an explicit flush or
shutdown, outside state transactions, with a maximum five-second wait. Unavailable
disk and permanently blocked exporters surface typed health errors. Disk-unavailable
health cannot itself always be persisted; in-memory fallback is exposed to callers.
When a flush exhausts its budget, its report leaves `pending_count` unknown and
records timeout degradation in memory without further database work. Query health
separately for the current count. This timeout signal may be lost on process exit;
pending span records remain durable. A zero-budget flush performs no database work.

`telemetry.export_offline(Path("data/trace-export"))` writes sanitized `spans.jsonl`
and a checksummed `manifest.json` without removing pending spans. Verify the hash
before loading lines as versioned `SpanRecord` objects; enqueue them into another
outbox to replay. Replay preserves the original SDK identifiers and is at-least-once:
an acknowledged batch retried after an acknowledgement/write failure may duplicate
delivery. Collector/UI deduplication is not assumed; Task 9 verifies actual behavior.

Local startup (optional; never part of default tests):

```sh
docker compose -f observability/docker-compose.yml up -d
uv run python -m app.observability.health
```

The health command probes `/healthz` and returns JSON plus exit 0/1. Liveness alone
does not prove span ingestion. `Telemetry.health()` reports local queue health and
degradation separately. `FINANCE_PHOENIX_ENDPOINT` may select another literal
loopback IP/port, with exact `/v1/traces` path; credentials, query strings, fragments
and remote hosts are rejected. Ambient `OTEL_*` headers and proxy variables cannot
override this application boundary. Compose exposes only loopback HTTP port 6006,
disables Phoenix UI analytics/external resources, and preserves its named volume.
Editorial authentication uses the separate browser service described below; local
Phoenix development authentication is disabled and must not be exposed publicly.

No real model, agent golden evaluation, or authenticated editorial approval is
provided by tracing. Offline span evidence validates infrastructure behavior only.

## Local browser reviewer login (Task 11)

The identity boundary supplies sessions for the authenticated content/release
review routes described below. No publishing routes are installed. `ReleaseService` remains default-deny unless its caller
explicitly supplies `SessionService.verify_principal`; The review service wires the authenticated human decision routes. The verifier uses the same SQLite connection and only
reads session state, including when called inside a business transaction.

Register your own GitHub **OAuth App** in GitHub developer settings. Use homepage
`http://127.0.0.1:8765` and the sole callback
`http://127.0.0.1:8765/auth/github/callback`; disable wildcard redirects and do not
add other callback URLs. Request no repository, email, refresh or offline scopes.
Obtain the intended human account's stable numeric GitHub ID from GitHub's public
user API or your own operator CLI lookup. A display login is never an allowlist
key. Bots and organizations are denied. There is no first-login enrollment.

In your ignored `.env`, set `FINANCE_GITHUB_CLIENT_ID`,
`FINANCE_GITHUB_CLIENT_SECRET`, and `FINANCE_GITHUB_ALLOWED_IDS` (comma-separated
positive integers), and explicitly set
`FINANCE_AUTH_LOOPBACK_DEVELOPMENT=true`. Never paste credentials into chat,
command history, logs or checked-in files. Load the file explicitly with
`uv run --env-file .env`; application code never reads `.env` automatically.
Missing any credential or the allowlist produces `authentication_setup_required`
at `/auth/status` and disables login. There is no gh, PAT, anonymous or test-auth
fallback.

Embedding example (save as an ignored local script; do not run a generic Uvicorn
CLI with access logging enabled):

```python
from app.config import Settings
from app.storage.db import open_database, migrate
from app.auth.github import GitHubOAuth
from app.auth.sessions import SessionService
from app.web import Services, serve

settings = Settings.from_env()
settings.data_dir.mkdir(parents=True, exist_ok=True)
conn = open_database(settings.data_dir / "pipeline.sqlite")
migrate(conn)
try:
    serve(settings, Services(oauth=GitHubOAuth(settings),
                            sessions=SessionService(conn, settings)))
finally:
    conn.close()
```

Open `/auth/login` in the browser. The five-minute, one-use state is bound to an
HttpOnly browser cookie and S256 PKCE. Pending handshakes are memory-only and
invalidate on restart. Only the synchronous GitHub exchange runs in a worker
thread; SQLite verification, revocation and session creation stay on the serving
event loop thread, so slow GitHub responses do not block logout/session checks. The callback discards GitHub tokens and rotates the local
session, then redirects to the setup status page. It does not approve anything.
`GET /auth/session` checks current identity and refreshes the CSRF cookie;
`POST /auth/logout` requires the session cookie, exact
`Origin: http://127.0.0.1:8765`, and `X-CSRF-Token` matching the `finance_csrf`
cookie. CSRF tokens are HMAC-bound to the opaque HttpOnly session, never persisted.
Restart changes the CSRF key; fetch `/auth/session` before a subsequent mutation.

Sessions expire after 1,800 seconds idle or 28,800 seconds absolute. DB rows store
only session-cookie SHA256, random server reference, numeric ID, timestamps and
revocation. Current allowlist membership is rechecked on every request and verifier
call; removal immediately denies the existing session. Display login, email,
GitHub response bodies, OAuth credentials, state, PKCE and CSRF are not persisted.

`serve` binds only `127.0.0.1:8765`, uses one process, disables proxy-header trust
and all Uvicorn access logging. The app rejects unexpected Host, scheme and all
forwarding headers, has no CORS middleware, disables automatic slash redirects (noncanonical callback
paths return 404), suppresses callback query data in its ASGI scope, and emits no authentication traces or application logs. Do not add
request/query/body logging, automatic HTTP/ASGI instrumentation or proxy wrappers
around this app. Cookies are HttpOnly and SameSite=Lax (the CSRF cookie is readable
by the same-origin UI). Non-Secure cookies are supported only by the explicit
HTTP loopback development mode; alternate origins/callbacks fail setup. No remote
hosting or HTTPS deployment is implemented in this slice.

The trust boundary is a local trusted machine. This does not protect against a
malicious OS administrator, access to the browser profile, modified local Python
code or direct writable SQLite access. Keep database files and `.env` private;
share only sanitized evidence exports. Never run this service on a shared or
untrusted workstation.

Mocked CI tests exercise OAuth/session security and inspect raw SQLite snapshots,
SQL exports, debug application/HTTP transport logs and an in-memory OTel exporter
for credential/private-identity sentinels. Real GitHub browser login requires
your OAuth app and has not been exercised by this implementation task.

### Authenticated browser review (schema 1.0)

The backend can expose existing artifacts by adding `ReviewService` to the
embedding example above. Construct `ArtifactStore(settings.data_dir / "artifacts",
conn)`, `PolicyEngine(store)` and `StateService(conn, store, policy)`, then construct
`ReviewService(state, current_session_verifier=sessions.verify_principal)` and pass
it as `Services(..., review=review)`. Import these types from
`app.storage.artifacts`, `app.workflow.policy`, `app.workflow.state` and
`app.review.service`. Keep every service on the same SQLite connection and owning
serving thread. A fresh `PolicyEngine` has no configured validators and denies
approval; only explicitly configured, versioned validators can establish readiness.
Foundation demo validators accept explicitly synthetic fixtures only and do not establish editorial readiness.

Run the separate read-only navigation process from the repository root:

```sh
uv run streamlit run app/ui/review.py --server.address 127.0.0.1 --server.port 8501 --server.enableCORS true --server.enableXsrfProtection true --browser.gatherUsageStats false
```

The checked-in `.streamlit/config.toml` also sets these defaults and disables
Streamlit usage telemetry. `FINANCE_REVIEW_DATABASE` selects the existing database
(default `data/pipeline.sqlite`). Its URI uses `mode=ro` plus `query_only`; opening
navigation never creates or migrates a database. Streamlit only displays status,
synthetic markers and top-level links to `http://127.0.0.1:8765/review/{story_id}`
and `/releases/{candidate_hash}`. It never reads browser cookies, forwards tokens
or asserts a reviewer identity. Sign in through the backend `/auth/login` first.

The authenticated backend independently renders narration/script, claims,
sources/locators, verification, captions/subtitles, charts, attributions and the
asset/license register. Retrieved strings are escaped; only HTTP(S) source URLs
become links. Media is authenticated, resolved from a current manifest member's
immutable hash and served with its registered safe MIME type. HTML/SVG, arbitrary
paths, stale member hashes and corrupt content are denied. CSP permits same-origin
images/media and native forms; scripts and frames remain disabled.

Native forms POST a bounded, strictly URL-encoded body. Exact Origin and a
server-generated session-bound CSRF token are mandatory. Duplicate fields, actor
fields, duplicate CSRF headers, ambiguous header/form tokens, malformed encoding
and oversized bodies fail closed. Existing header-CSRF clients for logout remain
supported. CSRF/session tokens stay outside URLs, DB audit, traces and logs.
Each rendered form has its own key: browser retry/resubmission must preserve the
submitted key and complete payload. Do not fetch a new form to retry a response
lost after commit. Changed payload under the original key conflicts.

Content decisions recheck current session/allowlist, expected version, exact
manifest and current evidence/rights under `BEGIN IMMEDIATE`. Approval, decision,
state event and receipt commit atomically. Reject/revise can handle a non-ready
manifest; approve has no readiness override. Any member change revokes the active
approval while preserving old audit rows. Factual/script changes return to
verification; visual changes return to rendering. Historical receipts never
reactivate revoked content or releases, and responses show current status
separately. Content approval cannot approve a model/prompt release. First release
bootstrap requires its own explicitly checked human action. Release checks re-read
all split/repeat evidence, baseline and exact pins under the same write lock.

Human identities and notes remain in the protected local audit database. The review service queues a local `approval.decision` span containing the opaque decision ID after checking readiness. Receipts in schema 1.1 record the trace ID only after the outbox accepts the span. If telemetry fails, the decision can still commit with a null trace ID and status `DEGRADED`. Historical 1.0 IDs are unverified. An idempotent replay reads the original receipt.

The business journal and telemetry outbox use separate transactions. A later business rollback can leave a span for an attempted decision. Only a committed receipt and state event establish the result. Local enqueue by itself makes no claim of collector delivery. Native FastAPI instrumentation stays
disabled, including callback query tracing. There is no test-auth configuration or
browser-provided actor identity, and no publication route or adapter. Approved
synthetic artifacts are not publishable media.

Initial migrations 001/002 now use `WITHOUT ROWID` to close hidden
`rowid`/`_rowid_`/`oid` replacement aliases; 007's immutable audit tables do too.
This initial-DDL hardening applies to **fresh schemas only**. Existing databases
that already applied 001/002 are not rebuilt, deleted, or silently repaired;
operators must not treat their old DDL as hardened. A reviewed data-preserving
upgrade remains future work before existing databases are used for review.

The synthetic integration suite exercises mocked GitHub callback/session flows,
exact approval, form security, competing edit/approval connections, media isolation,
release bootstrap/retry, audit rollback and historical replay. Live GitHub browser
smoke (sign in → review → approve → edit → observe revocation) still requires real
local OAuth credentials and has not been run. No service, real agent, paid provider
or publication is started by the tests.

## Local monitoring template (synthetic foundation only)

The existing read-only Streamlit navigation includes a **Monitoring** page.
Run the existing local UI command with `.streamlit/config.toml`: it binds
127.0.0.1:8501, disables browser usage statistics, and retains CORS/XSRF protection.
Review links still open the authenticated backend on 127.0.0.1:8765; the monitoring
page has no credentials, cookies, decision actions or alert delivery integration.
No service is started by a query or by this documentation.

Set `FINANCE_REVIEW_DATABASE` to the migrated execution/review SQLite database,
`FINANCE_TELEMETRY_DATABASE` to the **separate** telemetry database, and optionally
`FINANCE_MONITORING_EVAL_DIR` to the local directory containing immutable experiment
bundle JSON files (default `data/evals`). Invalid/missing files become explicit
missing-data indicators. All database reads use SQLite `mode=ro` and `query_only`;
a missing database is not created. No ordinary dashboard read queries Phoenix or
any network service.

`MonitoringRepository.summary(agent_name, version, since)` returns a versioned
`AgentSummary`; `run_index(run_id)` returns a versioned `RunIndex`. The window
includes attempts with `started_at >= since`; `since` must be timezone aware.
Attempts, unique logical requests, completed attempts, failed/blocked/interrupted
attempts, retry attempts (`retry_count > 0`), invalid-input/schema and policy-denied
counts come from typed journal records. Corrupt records are counted separately as
**unattributed** because their agent/version/time cannot be trusted. Later agent
slices must add their actual typed output decoder; only the foundation's
`SyntheticData` envelope is decoded in this template.

Latency is each finished attempt's wall-clock duration, including handoff, never
the sum of child model spans. Percentiles use **nearest rank**, index
`ceil(p * sample_count) - 1`; no samples means null percentiles and sample count 0.
Usage comes only from attempt envelopes, never also from model-call spans. Token
totals remain null unless every selected attempt reports the respective count.
`usage_samples` counts attempts reporting both token counts; missing attempts are
counted explicitly. Costs are **known subtotals**, with measured and estimated
values and sample counts separate and currencies never converted or summed
across currencies. Empty cost maps mean no observed cost, not free execution.
Override rate is unavailable with denominator 0: the foundation has no automatic
approval override, and rejection/revision decisions cannot establish such a rate.

Evaluation summaries decode and validate versioned immutable local bundles and
expose only failure counts, critical false accepts and opaque bundle hashes.
Duplicate bundles are counted once. Window/version matching uses observed
execution records; empty or invalid evidence is unavailable, never a passing zero.
Records without output/time cannot be scoped safely: `eval_unscopable_records`
counts them plus missing expected execution slots (including empty incomplete
bundles), `eval_result_count` reports observed evaluator outcomes, failure and
false-accept totals remain null, and `eval_scope_incomplete` explains coverage.
Error/incomplete bundles also surface a local eval alert; missing evaluator
coverage cannot become a passing zero.
Holdout examples, answers, source text, local evidence paths, reviewer identities,
notes and callback credentials are excluded from summaries/indexes. The run index
reads protected history only as opaque event IDs and decision counts, without
reading protected event/decision payloads. It links known story/run/trace/span IDs
to opaque experiment bundle hashes. It does not infer Phoenix remote experiment
IDs from local bundle hashes.

Phoenix 21 has one owning project per global trace ID. The dashboard explicitly
labels pending span projects as **requested**, leaves verified owner and remote
URL null, and supplies stable local `trace:<id>/span:<id>` references. Experiment
routing metadata is not ownership evidence; dataset/experiment APIs are global.
Acknowledged outbox rows disappear. Therefore project filtering is explicitly
**partial, pending-span coverage only**, not a historical execution filter.
Evaluation rows match the selected invocation IDs, so another invocation in the
same run cannot inherit its project. Pending publishing signals deliberately
correlate at run level, separately from this evaluation scoping.
Journal trace/span links survive acknowledgement, with requested project unknown
when no pending evidence remains. No unified Phoenix navigation is claimed.

Alerts are deterministic, local surfaced projections, not persisted incidents or
external notifications. Exact typed envelope warning/error codes
`unsupported_claim_reached_review`, `missing_asset_rights`,
`forbidden_publishing_attempt` and `telemetry_failure` produce critical alerts;
arbitrary warnings/error text never appears. A pending `publish.run` span with
`policy_denied` also produces a local forbidden-publishing alert. Critical failed
or errored evaluator results and critical false accepts in the selected rows
surface an opaque `critical_eval_failure` alert with `scope=selected`.
Whole-bundle error/incomplete or unscopable coverage remains visible separately
with `scope=bundle_coverage`; it does not assert a selected-row safety failure. Telemetry corruption/unavailability and cumulative
loss/persistence/export failures surface local `telemetry_failure` alerts.
Telemetry health counters are **global/cumulative**, not scoped to the selected
agent or window; their alert reference and `scope=telemetry_global` say so. Pending publish signals are
windowed and correlated by selected run, and may disappear after acknowledgement.
No production finance agent or publishing adapter currently emits the first
three safety warning codes. Synthetic fixtures exercise this contract; complete
historical safety incident capture, real agent dashboards/threshold calibration,
remote ownership verification and external alert delivery remain future work.

### Local retention (manual only)

Run `.venv/bin/python -m app.maintenance --dry-run` to inspect a versioned JSON
report. Dry-run is the default; `--execute` explicitly enables deletion. No
schedule is installed. `FINANCE_DATA_DIR` selects the local root (default `data`).
The Python interface is `cleanup(now, dry_run=True, *, data_dir=None)`; `now`
must carry a timezone. Reports contain relative refs, reasons, byte counts and
safe error codes, never stored evidence bodies or exception messages. Exit 2
means errors require inspection; exit 0 means the inspection completed.

Only canonical `data/evals/<bundle_hash>.json` bundles with validated provenance,
completed record timestamps, a completed PASS/FAIL status and completion strictly
older than 30 days can be eligible. Exactly 30 days remains protected. Modification
times do not establish completion. Pending, corrupt, incomplete and unsupported
files remain protected. Business rows, ArtifactStore bytes, datasets, quarantine,
review/audit lineage and every historical candidate/baseline remain protected.
Candidate pins are verified against both stored evidence and ordinary/offline
source files. All supported database history contributes reference protection,
including revoked release history. No database rows are deleted.

Supported authority paths relative to the root are `state.db`, `telemetry.db`,
`evals/runtime/state.db`, and `evals/runtime/telemetry.db`. At least one valid
business database is required before deleting evidence. Other layouts require
an explicit future extension; this command never infers authority by recursively
opening arbitrary databases. Keep external registries in their owning layout;
cleanup cannot discover references from unrelated roots.

Dry-run opens existing SQLite files read-only with `immutable=1`, without creating,
migrating or enabling WAL. A nonempty WAL makes that read uncertain, so it reports
`reference_metadata_unavailable` and retains every candidate. Execute opens existing
databases with `mode=rw`, reserves their write locks and rechecks references before
each unlink. Changed files are retained. A failed unlink reports `deletion_failed`
and leaves all database evidence intact; successful deletions remain listed in a
partial report. Local processes must use the same authoritative databases; direct
filesystem replacement by an uncooperative process is outside SQLite locking.

Telemetry JSONL exports are snapshots of pending outbox rows, not delivery
receipts. Existing telemetry formats have no durable completed-export proof, so
all telemetry snapshots and pending rows remain protected. Complete offline eval
exports include immutable dataset copies and also remain protected in this slice.
No real scratch files should be removed to verify retention; tests execute solely
against temporary fixtures.
