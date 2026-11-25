# Scout OpenRouter Profile Evaluation & Spend Cap

Issue #7 evaluates News Scout clusters against named OpenRouter model profiles under a durable, atomic experiment spend cap while keeping `app/execution/openrouter.py` synthetic demo restrictions untouched.

## Profile catalog schema (`config/scout-openrouter-eval.example.toml`)

Catalogs are secret-free TOML documents validated by `app.scout.eval_config.ScoutEvalCatalog`:

```toml
schema_version = 1
default_profile = "ling"
hard_spend_cap_usd = "0.00"

[profiles.ling]
model_id = "inclusionai/ling-3.2-flash:free"
input_usd_per_million_tokens = "0"
output_usd_per_million_tokens = "0"
```

| Field | Type / Constraint | Notes |
| --- | --- | --- |
| `schema_version` | Integer `1` | Exact catalog schema version. |
| `default_profile` | Nonempty string | Must match a key in `[profiles.*]`. |
| `hard_spend_cap_usd` | Nonnegative finite decimal string | Shared cap across the entire experiment ledger (`"0.00"` default). |
| `profiles.<name>.model_id` | OpenRouter `author/model[:variant]` | Exact model ID; no traversal, query strings, or credential prefixes. |
| `profiles.<name>.input_usd_per_million_tokens` | Nonnegative finite decimal string | Ceiling for input token rate (USD / 1M tokens). |
| `profiles.<name>.output_usd_per_million_tokens` | Nonnegative finite decimal string | Ceiling for output token rate (USD / 1M tokens). |

### Environment variables & paid opt-in

- `FINANCE_SCOUT_EVAL_CONFIG`: Path to TOML catalog (default: `config/scout-openrouter-eval.example.toml`).
- `FINANCE_SCOUT_EVAL_PROFILE`: Active profile key (default: catalog `default_profile`, `"ling"`). Unknown profiles fail closed before any HTTP call.
- `FINANCE_ALLOW_PAID_PROVIDERS`: Must be `"true"` (or explicit `allow_paid_providers=True`) whenever `hard_spend_cap_usd > 0` or the selected profile has nonzero rates.
- `OPENROUTER_API_KEY`: Read from environment only when executing HTTP completions; never stored in `ResolvedScoutEvalConfig`, `config_hash()`, SQLite ledger rows, traces, or evaluation outputs.

## Pre-flight, enrichment, and atomic spend ledger

1. **Prompt & schema pinning**: `prompts/manifests/scout_enrichment.v1.json` pins `prompts/scout/enrichment.v1.md` by SHA-256 and verifies bytes before every request. Outgoing requests enforce OpenRouter `response_format` (`json_schema`, `strict: true`) for `ScoutEnrichment`.
2. **Pre-flight (`GET /api/v1/models/{model_id}/endpoints`)**: Before any completion, `app.scout.openrouter.verify_openrouter_endpoint` verifies endpoint availability (`status == 0`), `"structured_outputs"` support, permitted data use (`data_collection: "deny"`, no prompt training/retention), per-token USD rates converted to USD/1M `<=` profile rates, and zero extra/unbounded fees (`request`, `image`, `web_search`, etc.).
3. **Routing constraints**: Outgoing `POST /api/v1/chat/completions` requests set `provider.allow_fallbacks = false`, `provider.require_parameters = true`, `provider.data_collection = "deny"`, `provider.max_price = {"prompt": ..., "completion": ...}`, and send no `plugins` or billable add-ons.
4. **Atomic spend ledger (`app/scout/ledger.py`, `011_scout_eval_ledger.sql`)**:
   - Uses `BEGIN IMMEDIATE` in SQLite (integer pico-USD `10^-12` USD) across profiles, examples, threads, retries, and resumes.
   - Reserves worst-case cost (`pending`) before pre-flight/completion; rejects overruns (`SpendCapExceeded`).
   - Releases (`released`) **only** on confirmed pre-send non-billable rejection (e.g. pre-flight block).
   - Retains (`ambiguous`) on timeout or indeterminate remote/transport failure so encumbered budget stays protected.
   - Reconciles (`reconciled`) reported cost (or rate-derived cost) upon completion, including invalid JSON / schema / foreign-reference completions.
   - Never resets or increases `hard_spend_cap_usd` across profile switches or resumes for an existing `experiment_id`.

## Public evaluation boundary & output fields (for #9, #10, and #11)

Entry point: `app.evals.scout_runner.run_scout_eval(...)` (and CLI `python -m app.evals.scout_runner`).

### `ScoutEvalRunResult` fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | `"1.0"` | Envelope schema version. |
| `experiment_id` | `str` | Shared durable spend-ledger experiment identifier. |
| `profile_name` | `str` | Selected TOML profile name. |
| `requested_model` | `str` | Pinned OpenRouter `model_id` from the selected profile. |
| `resolved_model` | `str \| None` | Resolved model returned by OpenRouter when uniform across calls. |
| `resolved_models` | `tuple[str, ...]` | Distinct sanitized resolved model identifiers observed across calls. |
| `config_hash` | `Sha256` | Deterministic SHA-256 of `ResolvedScoutEvalConfig` (changes on profile switch; excludes `OPENROUTER_API_KEY`). |
| `prompt_id` / `prompt_version` / `prompt_hash` | `str` / `str` / `Sha256` | Pinned prompt identity and content digest. |
| `outbound_call_count` | `int` | Total outbound HTTP requests (`preflight_call_count + completion_call_count`). |
| `cost` | `Decimal` | Reconciled USD cost for this evaluation run. |
| `pending_reservations` | `int` | Count of `pending` reservations remaining in the experiment ledger. |
| `ambiguous_reservations` | `int` | Count of `ambiguous` reservations retained in the experiment ledger. |
| `ledger_summary` | `LedgerSummary` | Full ledger totals (`encumbered_usd`, `remaining_budget_usd`, `reconciled_cost_usd`, counts by status). |
| `calls` | `tuple[ScoutCallTrace, ...]` | Per-cluster call trace (`requested_model`, `resolved_model`, `parameters`, `input_tokens`, `output_tokens`, `cost`, `reserved_usd`, `reservation_status`, `failure_disposition`, `quarantined`). |
| `examples` | `tuple[ScoutEvalExampleOutcome, ...]` | Per-example run status, quarantine flag, `failure_dispositions`, token usage, and `SavedScoutOutput`. |
| `saved_experiment` | `SavedExperiment` | Scorable projection written to `saved_experiment.json` for `app.evals.scout.evaluate_experiment`. |
