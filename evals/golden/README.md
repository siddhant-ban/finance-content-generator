# Golden dataset starter

Use JSONL, one example per line. The file `starter_examples.jsonl` contains illustrative examples only; replace or expand them with adjudicated, source-grounded examples before using them as a release gate.

Required metadata:
- `example_id`
- `agent_name`
- `dataset_version`
- `split` (`dev`, `holdout`, `prod_regression`)
- `input`
- `reference`
- `labels`
- `severity`
- `source_refs`
- `adjudication`

Never tune prompts on `holdout`. Keep event-level leakage out of the split. Do not store full copyrighted article text.

The existing starter rows remain unchanged. [Their illustrative manifest](manifest.json)
records unadjudicated labels, the unresolved `ns-002` reference, missing locked
splits, and unreviewed rights. The loader rejects this dataset for all runnable
purposes; it cannot support promotion.

[Foundation 1.0.0](../foundation/1.0.0/manifest.json) is an original synthetic
harness dataset for the registered `foundation` agent, matching Task 6's fake
stage identity. Its payload schema IDs use the `foundation_fixture` namespace.
It contains three examples per split: an arithmetic success and two critical
policy traps. These small fixture counts provide no evidence of real-agent or
editorial quality. They contain no real news evidence or model-derived truth.

`load_dataset(root, split, purpose)` validates every pinned file, strict row,
manifest membership, event group and evidence fingerprint before returning.
Hashes provide local integrity pins, not author authentication; later release
approval must bind the canonical manifest `dataset_hash`. Versioned data must be
changed by creating a new version and updating its manifest, never by silently
editing an approved version. Evidence fingerprints are supplied grouping keys
for curated near duplicates; this loader does not infer semantic similarity.

`LoadedDataset.summary()`, ordinary repr and Pydantic serialization expose only
identity, split, purpose, count and hash. Evaluators explicitly call
`iter_examples()` to read selected inputs, references and labels. This API avoids
accidental holdout disclosure; local Python/file access remains privileged.
The core loader rejects `holdout` with purpose `tune`, independently of any CLI.

Fixture inputs wrap `SyntheticData(value: int)`. The `increment` scenario expects
`FakeStage`'s `value + 1` result. Other scenarios describe a deterministic wrapper
check: `invalid_reference` has an unresolved fixture evidence ID; `state_gate`
attempts a gated action from `DRAFT`; `changed_version` supplies `0.9.0` instead
of the pinned `1.0.0`; `missing_metadata` lacks required fixture metadata.
`source_refs` lists the fixture evidence IDs available to that wrapper.
`input.reference_ids` intentionally holds invalid IDs in negative fixtures;
top-level `reference_ids` instead links dataset examples and must always resolve.
An expected `reference.outcome = reject` is a successful policy rejection, not
an experiment error. No evaluator or execution wrapper is implemented here.

Rights and adjudication metadata are mandatory on the manifest and every row.
Synthetic rows require explicit fixture provenance; `adjudicated_agent` datasets
require reviewed rights and declared human adjudication. Only registered
foundation payload schemas are available in this slice. Real-agent schemas,
rights review, authenticating human labels, and release approval remain future
work; a metadata declaration alone cannot establish external trust.
