# OpenRouter in the synthetic foundation demo

## Intent

Demonstrate that a real provider response can pass through the foundation runner into the existing synthetic workflow. A successful live demo makes one free OpenRouter request, stores its validated result as the candidate's runner output, and ends at `AWAITING_APPROVAL`.

## Approaches

- Add `--openrouter` to the existing demo. The live response drives the candidate so the same run exercises the provider, typed output, traces, and review boundary. This is the selected approach.
- Add a standalone API smoke test. It would be simpler to run in isolation, but the synthetic workflow would still get its output from the fake provider.

## Design

- `--openrouter` opts the demo's first invocation into a small OpenRouter adapter behind the existing provider protocol. Use the existing HTTP client dependency and the free-only `openrouter/free` route. Read `OPENROUTER_API_KEY` from the process environment, which can be populated by `uv run --env-file .env` from the ignored local file. Keep credentials and raw API errors out of artifacts, reports, and traces.
- Send a versioned prompt and the synthetic input `4`. Validate the response as the typed arithmetic result `5` before creating a candidate. A missing key, provider error, or invalid answer fails the live demo. Make one request per live demo with no retries or fallback.
- Use the existing execution journal and trace export for the live invocation. Record the configured route and available token usage, and capture the resolved model in the trace when OpenRouter reports it. Keep the three foundation eval splits on their pinned fake configuration, with their existing release evidence and telemetry checks.
- The demo remains a synthetic test. Editorial agents, OAuth, approval, publication, and paid routing are outside this slice.

## Acceptance

An offline integration test with mocked HTTP verifies one request, the stored output linked from the candidate, and the final review state. Failure coverage verifies that no candidate is created after an invalid response or provider failure. Run the existing tests and offline evals, verify trace export, then run the opt-in demo once with the one-day key and check the persisted OpenRouter evidence.
