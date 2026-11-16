# Finance Media Automation — Codex + Superpowers Blueprint

An evaluation-first blueprint for building a mostly autonomous finance/economics news-to-short-video workflow. It is designed to be implemented in small, reviewable slices with Codex and the Superpowers GitHub skills framework.

## Product constraints

- Niche: finance and economics; explain what happened, why it matters, and what could happen next.
- Goal: side income, not a venture-scale platform.
- Publishing: **human approval is mandatory**. No agent may publish or bypass the approval gate.
- Budget target: under USD/CAD 50 per month (confirm currency and hosting choices before deployment).
- Prefer local/open-source components; paid model APIs are optional adapters, never a hard dependency.
- Factual verification must use source evidence, not model memory.

## What is in this blueprint

- `docs/ARCHITECTURE.md` — system boundaries, contracts, state machine, data flow and deployment.
- `prompts/agents/` — one independent system prompt per content agent.
- `prompts/ORCHESTRATOR_PROMPT.md` — end-to-end system orchestration contract.
- `docs/EVALUATION_PLAYBOOK.md` — shared golden-dataset, eval, regression and monitoring lifecycle.
- `evals/golden/` — starter examples and dataset schema.
- `observability/` — Arize Phoenix + OpenTelemetry design and local compose starter.
- `superpowers/IMPLEMENTATION_PLAN.md` — staged implementation tasks suitable for Superpowers.
- `docs/superpowers/specs/2026-11-09-foundations-design.md` — approved foundation design and architecture decisions.
- `docs/superpowers/plans/2026-11-09-foundations.md` — detailed test-first foundation tasks and acceptance gates.
- `AGENTS.md` — durable Codex project rules.

## Implemented synthetic foundations

The repository now implements strict contracts, immutable local artifacts, SQLite
workflow/audit state, a fake arithmetic runner, versioned synthetic evaluations,
durable sanitized telemetry, authenticated local review, monitoring and retention.
Finance agents, source retrieval, renderers and publishing adapters remain future
slices. No synthetic result establishes editorial quality or release approval.

From the repository root, with Python from `.python-version` and uv 0.13.4:

```sh
uv sync --locked
uv run --offline pytest tests/unit tests/integration -m 'not phoenix_live' -q
uv run --offline python -m app.demo --offline --output data/foundation-demo
```

The output directory must be empty or absent. The demo creates original synthetic
artifacts, all three evaluation splits, immutable offline bundles, a current review
manifest and sanitized trace evidence. It ends at `AWAITING_APPROVAL`; it cannot
sign in or approve anything. Inspect `data/foundation-demo/demo-report.json`
locally. Share only `public-report.json`, which contains aggregate synthetic
results and hashes, never holdout rows or authentication data.

See [the runbook](docs/foundations-runbook.md) for review embedding, offline
operation and retention. Phoenix is optional; default acceptance does not start
Docker or call external feeds/models. The existing optional live Phoenix probe
and real OAuth browser checks are separate evidence requirements. CI installs
locked dependencies, runs offline acceptance and uploads only the public report.
No remote CI execution or deployment is implied by the checked-in workflow.
