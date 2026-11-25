# Offline News Scout evaluation

Issue #4 scores saved outputs for the US prices, jobs and borrowing Scout. The
CLI and Python entry point work before the Scout worker and require no provider,
database or Phoenix instance. The evaluator checks discovery and attribution.
Novelty owns reel selection.

## Run the examples

From the repository root, with the development environment installed:

```console
python -m app.evals.scout --dataset evals/news_scout/1.0.0 --outputs evals/news_scout/1.0.0/samples/passing.json
python -m app.evals.scout --dataset evals/news_scout/1.0.0 --outputs evals/news_scout/1.0.0/samples/failed.json --baseline evals/news_scout/1.0.0/samples/passing.json
python -m app.evals.scout --dataset evals/news_scout/1.0.0 --outputs evals/news_scout/1.0.0/samples/regression.json
```

Exit 0 means the sample meets the scoring thresholds, 1 means it misses one or
more thresholds, and 2 indicates invalid or unavailable evaluation inputs.
The commands emit a JSON report on stdout. Save it locally alongside its inputs
to reproduce the scores. Dev and production-regression reports include
per-example reason codes and source IDs for missed critical pairs. Holdout
reports expose aggregate scores and counts. Report hashes identify the input
experiment, dataset manifest and policy.

The dev set has nine original fictional examples. Their locators identify
authored bulletins under the reserved `.invalid` domain. The dataset records the
rights and retention basis on every row. The production-regression split
contains a fictional syndication replay fixture. It can accept adjudicated
production failures in a new dataset version. The deliberately failing dev
sample combines false merges, a missed critical duplicate, fabricated metadata,
an incorrect disposition, malformed output, missing warnings and unsupported
claims, including a plausible claim with a valid source ID.

## Input and scoring contract

[The versioned Pydantic models](../app/evals/scout_contracts.py) define the
saved-output projection that issue #11 can populate from persisted Scout runs.
`SavedExperiment` pins the dataset version, canonical manifest SHA-256,
evaluator version, split, prompt version and model ID. Its `outputs` map uses
dataset example IDs. Each `SavedScoutOutput` gives the outcome, source
observations and candidate clusters. Preserve source metadata and disposition
even when an item cannot become a candidate. Pin each member and
representative with the source ID, revision and validated `Source.record_hash()`.
Save the projection with the original run artifact and provenance when
connecting a worker. The evaluator boundary is also available as
`app.evals.scout.evaluate_experiment`.

File pins hash canonical UTF-8 JSON with sorted keys, compact separators and
unescaped Unicode. The evaluator verifies dev, production-regression and
policy pins before scoring. It rejects cross-split reuse of event IDs,
near-duplicate group IDs, source IDs, source URLs or source fingerprints.
Assign event and near-duplicate groups when curating evidence, keeping related
coverage and revisions together. Record known external group links in
`related_event_groups` when a withheld item cannot join a candidate event. An unknown example ID invalidates the
experiment. A missing or malformed example output is a critical per-example
failure.

| Measure | Rule |
| --- | --- |
| Contract, metadata, reference integrity and deterministic policy | Each must score 100%. Check all source observations against their gold records and verify eligible membership, source pins, candidate revision, attribution, run outcome, warning accuracy and forbidden feed text. Any critical issue fails the sample. |
| Duplicate precision and recall | Compare unordered source pairs in saved clusters with adjudicated event memberships. Micro-average the pair counts across the split. Precision must be at least 0.95. Report recall, with a critical failure for each example that misses a gold pair marked critical. A zero denominator yields JSON `null`. |
| Audience relevance | Compare saved topics with the editor's topic labels for each expected event using F1. |
| Summary fidelity | Compare material statements with reviewed anchors and their permitted source IDs using F1. This version requires exact wording and the full required source set for each anchor. Unsupported statements trigger a critical policy failure. |
| Entity extraction | Compare entities with the reviewed entity set using F1. |
| Uncertainty | Compare typed correction, conflict and uncertainty signals with the expected set using F1. |
| Regression | `--baseline` rescored on the same pinned split compares rubric dimensions and duplicate metrics against versioned tolerances. An unavailable comparison metric fails the comparison. |

The [fixture policy](../evals/news_scout/1.0.0/policy.json) pins provisional
rubric floors and regression tolerances for the offline examples. Exact-text
anchors can reject a faithful paraphrase. This limitation must be measured
during editor calibration before interpreting the rubric as a release measure.

## Editor calibration and locked holdout

Dataset 1.0.0 records authored expectations. Editor calibration is **pending**,
and the manifest reserves the holdout. A fixture PASS produces local evidence
only. Real adjudication and new evaluation data go into a new version.

1. Have editors verify rights, locators, expected event groupings, attribution
   and critical duplicate labels. Record adjudication rationale, confidence,
   disagreement history and a nonprivate reviewer reference in each row.
2. Collect independent editor scores for the four rubric dimensions on saved
   dev outputs. Tie every score to the canonical SHA-256 of that example's
   output. Provide the saved dev experiment with `--calibration-outputs`.
   Calibration checks minimum matched coverage and mean absolute error by
   dimension. Include both credible and unsupported claims in the rated dev
   outputs. Agree on floors and regression tolerances on dev, then freeze
   the policy pin.
3. Have the holdout owner retain the untouched rows outside the repository and
   pin their canonical JSON hash using `holdout.status=sealed` and
   `holdout.sha256` in the new editor-adjudicated manifest. Keep the holdout out
   of tuning and prompt iteration. The evaluator opens the external
   `--holdout-file` only for a holdout experiment after dev calibration
   passes, then validates split isolation and emits aggregate evidence.
4. Bring offline results into the separate [release workflow](RELEASE_GOVERNANCE.md)
   when Scout release integration is available. This slice records scoring
   evidence. Authenticated release decisions and live worker trace delivery
   belong to that integration.

See the [evaluation playbook](EVALUATION_PLAYBOOK.md) for dataset governance
and the [issue](https://github.com/siddhant-ban/finance-content-generator/issues/4)
for scope.
