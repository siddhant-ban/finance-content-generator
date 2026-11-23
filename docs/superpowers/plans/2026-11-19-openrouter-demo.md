# OpenRouter Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this single dependent slice. Steps use checkbox syntax for tracking.

**Goal:** Feed a validated response from one free OpenRouter request into the existing synthetic demo.

**Architecture:** Add a narrow adapter behind the existing provider protocol and reuse the runner's typed output, journal, and traces. Bind a separate runner configuration and prompt for the candidate invocation while sharing the offline eval runner's storage and telemetry.

**Tech Stack:** Python 3.12, Pydantic, httpx, SQLite, pytest, OpenTelemetry.

**Spec:** `docs/superpowers/specs/2026-11-19-openrouter-demo-design.md`

## Global constraints

- Only `openrouter/free` may be requested. The live path is opt-in and makes one request per run.
- Read the key from the environment and send only the versioned arithmetic instructions and synthetic value. Keep secrets and raw failure text out of output and telemetry.
- Preserve existing offline eval provenance and the `AWAITING_APPROVAL` boundary.
- Use existing dependencies and test locally before attempting the live run.

## Review focus

- Missing credentials stop before the HTTP request.
- An unsuccessful API response makes one request and creates no candidate.
- A malformed or incorrect answer fails closed even when HTTP succeeds.
- A configured key by itself leaves the ordinary offline CLI free of network calls.
- A successful run identifies the live model where reported and keeps eval evidence pinned to the fake model.

## One vertical slice

**Files:** Add `app/execution/openrouter.py`, the live prompt and manifest under `prompts/`, and an integration test. Update `app/execution/protocols.py` and `app/execution/runner.py` for the provider and prompt checks, `app/demo.py` for the opt-in invocation, and `.env.example` and `docs/foundations-runbook.md` for local usage. Extend the trace sanitizer's attribute allowlist if needed.

**Interfaces:** `OpenRouterProvider.generate(request: ModelRequest) -> ModelResponse` implements the existing protocol. `run_demo(output_dir, telemetry_mode, *, openrouter=False) -> DemoReport` preserves its existing defaults. The CLI composes `--openrouter` with its existing telemetry selection.

- [ ] **Write failing tests.** Exercise the CLI and inspect its offline report, trace export, and SQLite journal with HTTP mocked. Assert exactly one POST with model `openrouter/free` and input `4`, a candidate referencing the stored `SyntheticData(value=5)`, the final state `AWAITING_APPROVAL`, available token counts and resolved model in the model span, and passing fake-pinned evals. Cover missing credentials and a default offline run with credentials. Parameterize API error, timeout, malformed output, and valid JSON with an incorrect answer and assert no candidate and no retry.
- [ ] **Confirm red.** Run `uv run --offline pytest tests/integration/test_openrouter_demo.py -q` and confirm the expected failure from the absent opt-in path.
- [ ] **Implement the adapter.** Use the fixed OpenRouter chat completions endpoint, an explicit timeout, and the pinned prompt. Strictly parse the response into `SyntheticData`; verify its value against the deterministic expected result. Return reported usage and sanitized resolved model metadata. Surface controlled failures without exposing response bodies or retrying.
- [ ] **Wire the live invocation.** Admit only the reviewed adapter under the matching pinned provider configuration and verified prompt. Construct the live runner with the existing store, telemetry, and settings. Keep experiment execution on the original fake runner. Include the live attempt in the existing offline trace coverage checks.
- [ ] **Document usage.** Show how to run with the ignored local `.env` via `uv run --offline --env-file .env python -m app.demo --offline --openrouter --output data/foundation-openrouter`. Document that the result is an integration check and free-tier availability can affect it.
- [ ] **Confirm green and review.** Run the targeted and full pytest suites, the offline demo, and inspect all split statuses and trace coverage. Request code review for the slice and address actionable findings.
- [ ] **Verify live acceptance.** Run the documented command with the one-day key and a fresh output directory. Check the stored candidate result, OpenRouter model span, token usage when reported, offline evals, and final state. Record any access blocker without printing credential values.
