# Run Scout on a recorded release

Run the offline Scout against the checked-in Bureau of Labor Statistics CPI recording from a checkout with the locked dependencies installed.

```sh
uv run --locked --offline python -m app.scout.cli run --request tests/fixtures/scout/bls-cpi-request.json --recording tests/fixtures/scout/bls-cpi.json --output data/scout-cpi
```

The UTC window is half open. Edit `window_start` and `window_end` in a copy of the request and use fresh run and invocation IDs for a new request. The SQLite journal binds both IDs to the original request hash. The CLI checks the normalized recording against the request's feed hash. The prompt and release identifiers in this request are fixture pins. The response and token counts are authored test data. The recorded source metadata, release locator and figures come from BLS. [CPI release](https://www.bls.gov/news.release/archives/cpi_09112025.htm) · [BLS copyright policy](https://www.bls.gov/opub/copyright-information.htm)

The command prints the batch disposition and each accepted candidate's summary, topics, entities, warnings and provenance. It writes `report.json`, the business journal `scout.sqlite3`, an immutable `artifacts/` store and `scout-telemetry.sqlite3`. The offline outbox contains sanitized spans joined by the report's trace ID. The command returns exit code 1 for a failed batch. Inspect `outcome` and `items` for partial coverage.

## Interfaces for other Scout tickets

| Interface | Use |
| --- | --- |
| `app.scout.run.run_scout` | Invoke a recorded or injected run with a `ScoutRunInput`, the artifact store and telemetry. The input artifact freezes the UTC window, topic and fingerprint snapshots, feed and configuration pins, release identity and limits before fetching. |
| `FeedTransport` and `Clusterer` in `app.scout.run` | Fetch returns a finite mapping of item IDs to raw `ScoutFeedItem` JSON values or bytes in feed priority order. The CLI uses item ID order and the graph validates payloads as it processes them. Clustering returns stable keys mapped to a partition of the eligible IDs. The default uses exact canonical URLs. |
| `Enricher` and `ModelResult` in `app.scout.run` | The adapter receives only the eligible pinned members, remaining wall-clock time and the combined input/output token ceiling. It checks the bound before calling and returns raw `ScoutEnrichment` JSON and token usage. Raise `TokenBudgetExceeded` for a call that cannot fit, `TimeoutError` when its deadline expires or `OSError` for an unavailable transport. |
| `ScoutRunOutput` in `app.contracts.scout` | Read the versioned batch envelope and its candidate artifact refs, frozen input ref, trace ID, reported tokens and item dispositions. `empty` means the run completed without a deliverable candidate. `partial` includes known unprocessed work or mixed successes and failures. |
| `StoryCandidate` in `app.contracts.scout` | Read directly with `StoryCandidate.model_validate_json(store.read(ref))`. Both workflow checkpoints reference these same immutable bytes. The candidate includes stable story and cluster UUIDs, revision, fingerprint, attributed statements, and each complete source record with its ID, revision and hash. |
| `register_scout_policy` in `app.scout.workflow` | Register the typed editorial candidate and cluster checks on a store-bound policy engine. These checks authorize the initial discovery checkpoints. |

The generated discovery and downstream schemas are in [schemas/v1/scout.json](../schemas/v1/scout.json). Registry and ingestion can supply the feed transport, the model ticket can supply the enricher, and the durable clustering ticket can replace the exact-URL clusterer. The editor view and Novelty both read the artifact named by the batch report.

## Current limits

- The CLI takes a normalized recording. Live RSS, real model calls, paid budgets, Scout evaluation data and human release approval are later slices.
- The initial worker writes revision 1. Repeat inputs reuse an identical delivered artifact. Retry an interrupted initial delivery with its original request IDs. A changed artifact for an existing story reports a revision conflict. Cross-feed near-duplicate rules, multi-revision delivery and full audit replay are later slices.
- Adapters must honor the supplied timeout. The graph checks elapsed time before starting work and discards late results. Synchronous adapter calls and local SQLite/filesystem commits use their own I/O timeouts and can finish after the work deadline.
- The fixture checks deterministic provenance, schema and workflow behavior. Factual quality requires separate evaluation. The current foundation artifact store requires POSIX directory syncing for production durability.
