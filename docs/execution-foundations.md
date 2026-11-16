# Synthetic execution and recovery

Task 6 runs synthetic typed stages only. Set `Settings(environment='test',
fake_stage_enabled=True)` (or `demo`); the default local configuration rejects
execution. No provider, agent, authenticated approval or publishing adapter is
implemented. `FakeStage` increments `SyntheticData.value` using a local fake
provider. These checks establish infrastructure behavior, never editorial truth
or finance agent performance.

`AgentRunner(store, telemetry, settings, prompt)` consumes a verified foundation
prompt and an `InputEnvelope[Payload]`. Stages expose the protocol metadata,
validation methods and `output_type` so saved envelopes can be safely decoded
without executing the stage again. Use a new logical envelope `invocation_id`
for deliberate reruns. Each actual attempt gets its own invocation UUID; logical
run/story IDs remain stable. Exact input, reference, metadata and model settings
are fingerprinted. Reusing a logical invocation with different inputs fails.

The runner durably journals up to three attempts. Only explicit transient provider
errors, timeouts and connection failures retry, sleeping 1 then 2 seconds. Clock
and sleeper are injectable. Input/output schema and policy failures do not retry.
Exceptions are represented by fixed safe codes; raw messages are not persisted.
Provider/model/finite known parameters are pinned in the attempt journal and safe
spans. Costs and tokens remain unknown when unavailable, including a multi-call
stage with any unmeasured call. Attempt-level usage is retained in each full
output envelope; final output usage describes the final attempt, not a total
across retries.

An optional constructor-bound handoff builder maps a validated stored output to
a `TransitionRequest`; a bound `StateService` executes it. The builder must bind
the same story and output reference, expected version, stable scoped idempotency
key and trace ID. `RunContext` exposes instrumented tool/provider operations and
usage, with no state, storage, approval or publication API. This is a Python API
capability boundary, not an isolation sandbox for hostile stage implementations.

Artifacts are durable before the state service is called. The journal remains
`STARTED` until the exact complete handoff payload and typed output envelope are
saved together as `OUTPUT_STORED`. A failure during the builder therefore cannot
silently become a completed transition. Recovery reuses the original saved
request, trace and idempotency key; it never rebuilds a request with a changed
callback. StateService receipts prevent a second state advance after a crash
following commit. Completed replay verifies the saved output artifact again.

Stop other runners before calling `recover_interrupted()`. It is an explicit
single-process restart operation, not a distributed lease or live-worker reclaim
mechanism. `STARTED` attempts become `INTERRUPTED` and cannot be rerun under the
same logical invocation. `OUTPUT_STORED` records can reconcile their saved trusted
handoff without rerunning the stage. A crash before its first attempt record, or
between transient failure and retry, requires a new logical request. There is no
exactly-once guarantee for external provider calls. No automatic retry continues
uncertain external work after restart.

Telemetry uses its own durable outbox; execution does not flush or contact a
collector. Output observability status is checked after attempt and root span
completion, so persistence failures can be reflected. Pending local telemetry
also reports `DEGRADED` until exported. `Telemetry.export_offline(directory)`
provides checksummed sanitized span bundles; full typed input/output envelopes
remain in the execution journal for future evaluation bundling. No editorial
golden dataset, rubric scoring or monitoring UI is supplied by this slice.

Run deterministic infrastructure checks without services:

```sh
.venv/bin/python -m pytest tests/unit/test_runner.py tests/integration/test_execution_recovery.py -q
.venv/bin/python -m pytest -q
```
