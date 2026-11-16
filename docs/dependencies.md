# Foundation runtime compatibility

Task 1 tested runtime: CPython 3.12.15 on macOS arm64. Initially supported Python
range: >=3.12,<3.13. Wider support is deliberately not asserted. uv.lock fixes exact
resolved packages/hashes; .python-version pins the exercised interpreter.

Primary documentation consulted 2026-11-10:
- [uv Python selection](https://docs.astral.sh/uv/concepts/python-versions/)
- [uv locking/sync](https://docs.astral.sh/uv/concepts/projects/sync/)
- [Pydantic strict validation](https://docs.pydantic.dev/latest/concepts/strict_mode/)
- [Pydantic serialization](https://docs.pydantic.dev/latest/concepts/serialization/)

Resolved initial dependencies: Pydantic 2.14.3, pydantic-core 2.50.4, pytest 9.2.0.
Strict Python validation forbids coercion; the explicit JSON boundary allows typed
UUID/date parsing and decimal strings. Extra fields and incompatible schema
versions are rejected. Do not serialize arbitrary provider responses into these
contracts. Phoenix/OTel/OAuth dependencies are not installed by Task 1.

## Task 5 observability pins

OpenTelemetry SDK/API and OTLP HTTP exporter are pinned to 1.45.2 in uv.lock.
The exporter now delegates HTTP operations through a transport client; tests
inspect actual prepared requests rather than relying on an old `_session` field.
Explicit SDK headers still merge environment headers in this version, so the
final local session enforces the header allowlist, disables proxy/netrc discovery,
rejects redirects and refuses partial-success acknowledgements with rejected spans.

The Phoenix 21.0.0 multi-platform index digest in compose was read from the public
registry on 2026-11-10 and verified against the SHA-256 of the returned manifest.
It supports Linux arm64 and amd64. No image startup or live trace visibility is
claimed by this compatibility check; Task 9 owns the live integration probe.

Settings format is now 1.1 to include a validated, explicitly configured loopback
collector endpoint. Existing invocation/schema wire format remains 1.0.

Primary references:
- [OpenTelemetry Python exporters](https://opentelemetry-python.readthedocs.io/en/stable/sdk/trace.export.html)
- [Phoenix project routing](https://arize.com/docs/phoenix/tracing/how-to-tracing/setup-tracing/setup-projects)
- [Phoenix privacy settings](https://github.com/Arize-ai/phoenix/blob/main/src/phoenix/config.py)
- [Phoenix health endpoint](https://github.com/Arize-ai/phoenix/blob/main/render.yaml)

## Task 9 client boundary

`arize-phoenix-client==3.7.0` is pinned without the full Phoenix Python server or
evaluation extras. `httpx>=0.27,<1` is an explicit runtime dependency, locked to
0.28.1. Existing OTel SDK/API/exporter 1.45.2 pins remain unchanged. The SDK's
verified wheel SHA256 is
`20a39e524a124ea48a61a6619f2997b938e9fe36996cf686ab2624aadbc3899b`.

Actual 3.7.0 source was inspected: `Client(http_client=...)` bypasses SDK
`.env.phoenix` and PHOENIX environment discovery; `Dataset.id`, `version_id` and
`examples` supply remote linkage; `experiments.create`, `log_run` and
`log_evaluation` register already executed evidence. The upload API may make
multiple bounded requests (upload and versioned readback). The adapter gives
each request an explicit one-second timeout. This is not an end-to-end batch
deadline. Only literal HTTP loopback addresses are allowed through
`FINANCE_PHOENIX_ENDPOINT`, with no redirects, proxies, netrc, ambient auth or
cookies. Tests inspect actual final HTTPX requests under credential sentinels.
The full server remains the immutable compose image; live compatibility is
reported separately, never inferred from a package install.

Primary references (verified against the pinned installed source):
- [Phoenix client3.7.0](https://pypi.org/project/arize-phoenix-client/3.7.0/)
- [Client initialization](https://arize-phoenix.readthedocs.io/projects/client/api/client.html)
- [Dataset API](https://arize-phoenix.readthedocs.io/projects/client/api/datasets.html)
- [Experiment APIs](https://arize-phoenix.readthedocs.io/projects/client/api/experiments.html)

## Task 11 browser authentication

Verified primary documentation on 2026-11-11:

- [GitHub web OAuth flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps): random state, S256 PKCE, exact redirect URI.
- [Authenticated user endpoint](https://docs.github.com/en/rest/users/users?apiVersion=2026-03-10): public account identity without repo/email scopes; REST version pinned to `2026-03-10`.
- [Authlib 1.9.5 HTTP client and PKCE documentation](https://docs.authlib.org/en/stable/oauth2/client/http/index.html).
- [FastAPI dependency injection tests](https://fastapi.tiangolo.com/advanced/testing-dependencies/).
- [Starlette TestClient](https://starlette.dev/testclient/): HTTPX2 is the supported test backend; plain HTTPX is deprecated.
- Exact release metadata: [FastAPI 0.162.4](https://pypi.org/project/fastapi/0.162.4/), [Authlib 1.9.5](https://pypi.org/project/Authlib/1.9.5/), [Uvicorn 0.54.0](https://pypi.org/project/uvicorn/0.54.0/), [HTTPX2 2.16.0](https://pypi.org/project/httpx2/2.16.0/).

Runtime pins are FastAPI 0.162.4, Uvicorn 0.54.0, Authlib 1.9.5 and HTTPX
0.28.1; development tests additionally pin HTTPX2 2.16.0. Starlette 1.10.0 is
locked transitively. The production OAuth adapter uses Authlib's installed
`prepare_grant_uri` and `create_s256_code_challenge` protocol helpers with an
explicit hardened HTTPX 0.28.1 client; it does not use Authlib's HTTPX2 client
integration. GitHub token exchange and identity lookup use fixed HTTPS URLs,
no redirects, a ten-second per-request timeout, no ambient proxy/netrc discovery,
and no cookies carried from token endpoint to identity endpoint. Explicit empty
scope avoids inheriting a previous authorization's scopes by omitting the scope
parameter. Tokens are not stored or refreshed.

Settings format is 1.2. Authentication configuration is excluded from execution
configuration dumps/hashes; changing browser credentials or allowed reviewer IDs
does not change model execution evidence. Existing execution envelopes retain
schema 1.0 and can decode historical configuration version strings.

[FastAPI's native OpenTelemetry documentation](https://fastapi.tiangolo.com/advanced/opentelemetry/)
was also checked against installed 0.162.4 source. Native tracing, metrics, logs
and operation spans default to enabled and reuse existing global providers.
The authentication app explicitly disables every native signal and automatic
configuration; a regression test installs a real global OTel provider and proves
that OAuth callback code/state generate zero exported spans.
