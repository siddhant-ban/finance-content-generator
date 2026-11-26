# News Scout OpenRouter dev comparison

**Decision for [Issue #12](https://github.com/siddhant-ban/finance-content-generator/issues/12): no candidate selected.** On November 26, 2026, all five exact free routes advertised structured outputs and zero token prices. None could be verified to meet Scout's provider data-use policy. The operator stopped the comparison at endpoint eligibility. It sent zero inference requests, incurred $0 in inference spend and used no substitute route. The holdout remained unopened. [Issue #11](https://github.com/siddhant-ban/finance-content-generator/issues/11)

## Ling smoke check

The first route checked was Ling. The proposed smoke input was the [recorded August 2025 CPI fixture](../tests/fixtures/scout/bls-cpi.json). It supplies one BLS source, its publication and retrieval timestamps, and a short permitted paraphrase located in the opening paragraph and Table A. BLS states that its release text is public domain. [Official CPI release](https://www.bls.gov/news.release/archives/cpi_09112025.htm) · [BLS copyright policy](https://www.bls.gov/opub/copyright-information.htm)

Ling returned HTTP 200 from the model endpoint listing, at $0 with structured-output support. The record lacked the endpoint's retention and training policy, and the exact free route was absent from the ZDR listing. The live smoke stopped before submit, with 0 of 1 planned inference requests. Live consumer acceptance and statement attribution remain unmeasured. The checked-in recording provides an offline control for the [Scout pipeline and typed Novelty consumer](../tests/integration/test_scout.py).

## Route evidence

Each linked model endpoint response contained one endpoint with status `0`, advertised `structured_outputs`, `response_format`, `temperature` and `max_tokens`, and listed prompt and completion prices of $0 per million tokens. These are catalog observations. Account-specific completion availability, rate limits and latency require an eligible inference request.

| Profile | Exact model endpoint | Provider tag | Metadata HTTP | In ZDR listing | Dev examples run / planned | Completion requests / retries | Inference spend |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: |
| ling | [inclusionai/ling-3.2-flash:free](https://openrouter.ai/api/v1/models/inclusionai/ling-3.2-flash:free/endpoints) | `antdigital/fp8` | 200 | No exact match | 0 / 9 | 0 / 0 | $0 |
| ernie | [baidu/ernie-5.5-flash-preview:free](https://openrouter.ai/api/v1/models/baidu/ernie-5.5-flash-preview:free/endpoints) | `baidu` | 200 | No exact match | 0 / 9 | 0 / 0 | $0 |
| lfm | [liquid/lfm-3-1.7b:free](https://openrouter.ai/api/v1/models/liquid/lfm-3-1.7b:free/endpoints) | `liquid` | 200 | No exact match | 0 / 9 | 0 / 0 | $0 |
| particle | [particle-ai/particle-1:free](https://openrouter.ai/api/v1/models/particle-ai/particle-1:free/endpoints) | `particle-ai/us` | 200 | No exact match | 0 / 9 | 0 / 0 | $0 |
| nemotron | [nvidia/nemotron-3.5-super-vl:free](https://openrouter.ai/api/v1/models/nvidia/nemotron-3.5-super-vl:free/endpoints) | `nvidia` | 200 | No exact match | 0 / 9 | 0 / 0 | $0 |

The [Scout provider contract](scout-openrouter-eval.md) calls for no prompt training or retention. OpenRouter publishes ZDR eligibility at the endpoint level. The unauthenticated `GET /api/v1/endpoints/zdr` returned HTTP 200 with 948 entries. Comparing the five requested IDs against `data[*].model_id` returned zero matches. A second lookup for the model family in both `model_id` and `name` found priced LFM, Particle and Nemotron variants. OpenRouter's free-model catalog specifically says LFM3-1.7B free prompts and outputs may be retained and used for training. For the other four routes, eligibility was unverified. [ZDR listing](https://openrouter.ai/api/v1/endpoints/zdr) · [OpenRouter ZDR documentation](https://openrouter.ai/docs/guides/features/zdr) · [Free-model catalog](https://openrouter.ai/collections/free-models/)

Each profile has zero model outputs to score. Schema validity, unsupported claims, reference validity, rubric fidelity, uncertainty, completion latency, token usage and measured or estimated per-completion cost are unavailable for all five. No HTTP 429 was observed during metadata retrieval. Completion throttling is untested. There were no inference reservations or provider usage responses from which to derive ledger or billed-cost evidence.

## Frozen comparison inputs

Integration base: `e89778b93ce8465b8367cab1fdc5e061ad33c7d1`.

The planned comparison uses the nine [dev examples](../evals/news_scout/1.0.0/dev.json), including `wire-overlap`, `corrected-release` and `conflicting-measures`. Use the same dev input projection, `cluster_items` from [Scout clustering](../app/scout/cluster.py), and [evaluation runner](../app/evals/scout_runner.py) for every eligible route. The planned repeat count is one per dev example per profile, following the separate one-item Ling smoke. The common settings are temperature `0.0`, a combined input/output ceiling of 1,500 tokens per example, an output limit derived from that remaining budget and the adapter's input-size estimate, a 30-second run timeout and zero client retries. Use one experiment ID and one isolated SQLite ledger for the smoke, all profiles and any resumed work under the $0 shared cap. Use a distinct output directory per run.

Before a live run, verify retention eligibility for the exact route and ensure the request or account enforces it. The current [OpenRouter adapter](../app/scout/openrouter.py) accepts absent policy fields and sends `data_collection: deny`. OpenRouter exposes a separate ZDR restriction. Also verify the effective per-cluster `max_tokens` values are identical across profiles. The runner deducts prior clusters' reported usage from the budget, which can produce different limits in later requests. A launch with unequal limits cannot support a like-for-like quality comparison.

The [1.0.0 evaluator](SCOUT_EVALUATIONS.md) has exact-text fidelity anchors and pending editor calibration. Some facts have locators into fictional bulletins without source excerpts. For example, the corrected-release source has revision and locator metadata with a null excerpt. Treat this input coverage as a limitation if comparing live fidelity. The manifest marks the holdout `reserved`.

| Input | SHA-256 |
| --- | --- |
| Canonical dataset manifest | `d91f0bd278f2ba34a2d5ff4b3c4e3ad196c6405a69b03477c7ff62095e6d1815` |
| Canonical dev examples | `9db9b1878a617adf348d8c63adffe388db391cfac4dee344f88f0b60d9672e58` |
| Canonical scoring policy | `71f2315419fc75d3cffbf0aa80efad59db62286dc39d70ade32a8070d012c923` |
| Raw `scout_enrichment.v1` prompt | `aab972bdf2f6ef94bdfdbd7913dfa97c2f27ce04f2f20dd364d1fcc1f2ef94b5` |
| Strict enrichment JSON schema | `b16ec63032113322c60b5d1109788649b9b5aeed70e0ef35e5b1e184aa7f09a2` |
| Canonical CPI recording | `b27e0ee8ae831e8c8a7a665c829eb96e92fb03238980c51bd65a7bbb84969488` |
| Canonical CPI request | `c8bab86a3e85b94946499a6425ce0c798b23bc2b7dfcb74782cb74100d577344` |
| Canonical [profile catalog](../config/scout-openrouter-eval.example.toml) | `20ce955e0a9a66e272c8a33d6995f69e025983f1f64f9181bcd0be15cd04afe8` |

| Profile | Resolved config SHA-256 with paid providers disabled | Endpoint response body SHA-256 |
| --- | --- | --- |
| ling | `bffbf44c8a966482368544cc9f2f55f075fdcb88fd6d82240ef1ddec230c6fc6` | `7dc039cd14997398eac5d998e53bf251102c24ed713f979766d917849cbf0b5a` |
| ernie | `7fa73ea0a61f143b131ed9e80bd6197d294c944d0e816f355fc0dda4b365a725` | `5ce10d142939daf14184b87d5afdf4ae670b87661ac3aa06c18827b5d9a4d7bb` |
| lfm | `364a1688ea89be7b94885d768d073016811aeb47bdb1e54fc361e3fc3a93687f` | `855db42a3a12c73bed3734fddd9e77df592b38b932eeba219ecc5e948a79a16d` |
| particle | `2dc066559e24866960dc6588ab7343e8b9d1bc4c9a9f7b9989d3b331cbdde494` | `f643784e43d7f158b91e18a5a692433ff2bd09ce17417587adbf5ac4309d8f5f` |
| nemotron | `8a3ce76a15313f06535a8dd0fd888268eb569909d8bd23ad1a980a2ca76e924c` | `da24b63855b9c289e692905f26cbd34d853a699ae183e21c3fa303e8348901c6` |

ZDR response body SHA-256 was `465878a021b615c72aae2eb1043476e74d22566123a059bab647d426f0c08d5c`. These retrieval fingerprints cover the observed UTF-8 bodies, including volatile uptime fields. The tables preserve the fields used for the decision.

## Offline controls

The [authored passing projection](../evals/news_scout/1.0.0/samples/passing.json) scored PASS on dev, with nine examples, zero critical issues, all deterministic checks and rubric metrics at 1.0, and both expected duplicate pairs recovered. The scorer reported `release_eligible=false`. The projection pin was `edace7191f5a28755765230894eb3c89053c7f3ce1dfc7e16b7fc072d8c57792`. These are control results from the offline evaluator.

On the integration base, 95 Scout unit and integration tests passed in the Windows run. That includes the linked-trace and simulated Phoenix-outage recovery tests. Two recorded CPI tests initially hit Windows directory-sync errors. Both passed when rerun with the repository's existing test-only Windows directory-sync fixture. The consumer check verified the editor-visible candidate, persisted typed artifact and statement-level source pin. The full suite and the Scout boundary test module could not collect on Windows because the foundation imports POSIX `fcntl`. Live Phoenix was outside this comparison.
