# CI Test Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce median required PR CI wall time by at least 80%, run fewer tests, and enforce at least 90% line coverage across `app/`.

**Architecture:** Measure the existing Ubuntu job before changing its configuration. Use line coverage to enforce the floor and a scoped mutmut comparison to verify test removals. Optimize measured bottlenecks and compare the finished job with the current workflow under routine PR conditions.

**Tech stack:** GitHub Actions, Ubuntu, uv, locked Python 3.12, pytest, Coverage.py, and mutmut.

**Spec:** [CI test reduction with mutmut](../specs/2026-11-19-ci-test-reduction-design.md)

## Global constraints

- Use isolated checkouts on a common application revision. Keep the in-progress OpenRouter changes out of the experiment and out of CI commits.
- Use at least five successful Ubuntu timing runs per variant. Record queue time separately. Compare the current configuration with routine candidate runs, and report cold-cache behavior separately if caching is enabled.
- Keep the synthetic acceptance run, retention dry run on a fresh absent root, sanitized report upload, and direct safety checks in required PR CI. Mutation testing runs on demand on Linux.
- Preserve every baseline mutant kill in the affected source scope. Keep direct tests of distinct contracts and failure modes. Keep application behavior and agent eval fixtures intact.

## Review focus

- An unimported application module must count toward coverage, and a result just below 90% must fail in Task 2.
- A candidate mutation run with missing results, timeouts, tool errors, or a lost baseline kill must fail the pruning comparison in Task 3.
- A cold or lockfile-miss run must still install locked dependencies and pass all required checks in Task 4.
- Synthetic acceptance, retention isolation, and the public-only artifact must survive timing optimizations in Task 4.
- An incomparable or incomplete timing sample must be excluded and replaced, with the reason recorded in Tasks 1 and 4.

## Task 1 — Establish the Ubuntu baseline

**Files:** Read `.github/workflows/ci.yml`, `.python-version`, `pyproject.toml`, and `uv.lock`. Store working notes and raw evidence under ignored `data/ci-benchmark/`.

**Produces:** One timing row per run with variant, run URL, application revision, runner image, Python version, cache mode, queue seconds, job seconds, step seconds, and conclusion. A separate diagnostic record contains collected and selected test counts, collection time, slow node IDs, and line coverage by file and overall.

- [ ] Capture the common revision and benchmark the existing `Foundation offline acceptance` job with its current workflow definition and cache disabled. Reuse equivalent GitHub Actions run data where available. If more runs are needed, use the existing workflow against the pinned baseline on an isolated branch when remote execution is available. Record any missing runner access before changing the required workflow.
- [ ] Select at least five successful comparable runs. Compute wall time from job start to completion, including post-job steps. Record run creation to job start separately. Exclude failed, canceled, and mismatched runs with the reason and obtain replacements. Compute the median and the target of `0.20 * baseline_median_seconds`.
- [ ] On Ubuntu, synchronize locked dependencies and run the existing offline pytest selection with `--durations=25 --durations-min=0`; time collection separately with `--collect-only`. In a separate diagnostic environment with a pinned Coverage.py version, use `coverage run --source=app -m pytest tests/unit tests/integration -m 'not phoenix_live' -q` followed by `coverage report --show-missing`. Keep instrumentation out of the baseline timing sample.
- [ ] Identify which steps dominate and estimate the minimum attainable wall time if each candidate bottleneck were eliminated. Record the test group selected for the pruning pilot and the evidence that it overlaps other tests.

## Task 2 — Enforce the coverage floor in required pytest

**Files:** Modify `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml` and the CI or testing section of `README.md`. Change only measured coverage-gap tests if the diagnostic suite is below 90%.

**Consumes:** Task 1 coverage by file. **Produces:** The required `Offline tests` step exits nonzero on a pytest failure or line coverage below 90%.

- [ ] In a temporary Linux checkout, add a disposable, unimported module under `app/` with enough executable statements to drop total coverage below 90%. Run the existing offline pytest command and confirm it exits successfully despite the coverage drop. Verify the module appears at 0% using the diagnostic coverage report.
- [ ] Add Coverage.py to the `dev` dependency group and refresh `uv.lock`. Configure `source = ["app"]` and `branch = false` under `[tool.coverage.run]`. Configure `fail_under = 90`, `precision = 6`, and `show_missing = true` under `[tool.coverage.report]` so a near miss at this repository's size cannot round up to 90%. In CI, run `uv run --offline coverage run -m pytest tests/unit tests/integration -m 'not phoenix_live' -q` followed by `uv run --offline coverage report`, with the existing shell failure propagation. Document the same commands for local Linux use.
- [ ] Rerun with the disposable module and confirm the report lists it at 0% and exits with coverage failure. Check 89.9% fails and 90% passes in an isolated coverage fixture. Remove the disposable module. If the real suite falls below 90%, add tests for real uncovered behavior identified by the diagnostic. Demonstrate each new test catches the relevant regression, then restore the correct behavior and rerun it. Never commit the disposable module or a simulated regression.
- [ ] Run the exact CI coverage commands on clean Linux. Confirm pytest and the report exit successfully, application line coverage is at least 90%, and the test and eval summary is recorded. Review the slice and commit its files independently.

## Task 3 — Prune a measured hotspot with mutmut

**Files:** Modify only the test files identified by Task 1 and document the reproducible commands and comparison procedure in `docs/foundations-runbook.md`. Add mutmut copy settings or a `/mutants/` ignore rule only if the chosen Linux setup needs them. Keep logs in ignored `data/ci-benchmark/`.

**Consumes:** Task 1 hotspot and Task 2 coverage gate. **Produces:** A smaller retained test group, its before/after timings, and baseline and candidate tables keyed by mutant ID with status values.

- [ ] On Linux, pin a mutmut version that works with the locked Python and pytest. Record the version, source paths, offline pytest selection, and exact run and export commands. Choose mutation scope from the behavior of the selected tests, including their asserted production behavior rather than only their test file names. If the runner needs checked-in prompts, eval fixtures, or schemas, copy the necessary fixture paths and confirm the unmutated test selection passes in mutmut's working tree.
- [ ] Run the original offline suite against that scope and save the baseline mutant IDs and results. Run each proposed pruning batch in a separate clean mutation workspace with identical application bytes and tool settings. Regenerate result state so changed tests are actually executed. Confirm both mutant ID sets match.
- [ ] Verify the comparison detects a deliberately omitted ID, a `killed` result replaced with `survived`, and an unresolved timeout or tool error before accepting the genuine results. For the genuine run, require an identical mutant set and `killed` status for each baseline kill. Investigate unresolved statuses in either run and retain any direct test with a distinct contract or failure mode.
- [ ] Rerun the exact required coverage command, compare selected case counts and repeated pytest durations under the same conditions, and run the synthetic acceptance demo and retention dry run. Keep a pruning batch only if it reduces selected tests and improves runtime while passing the coverage and mutation checks. Review and commit validated pruning independently. If no candidate qualifies, record the pilot result and keep its tests.

## Task 4 — Optimize and verify the whole PR job

**Files:** Modify `.github/workflows/ci.yml` only for measured improvements and update `README.md` or `docs/foundations-runbook.md` with the outcome. Keep timing samples under ignored `data/ci-benchmark/`.

**Consumes:** Task 1 timing table, Task 2 coverage gate, and Task 3 pruning evidence. **Produces:** A verified candidate CI job and a before/after report.

- [ ] If dependency setup dominates, benchmark changing setup-uv from `enable-cache: false` to `enable-cache: true` with `cache-dependency-glob: uv.lock`. Keep `uv sync --locked` and pinned Python verification. Show the before run has no restored cache, then verify the candidate on a primed cache and on a fresh cache key. Check that a changed dependency hash cannot select the previous exact key and that the locked install succeeds with a cache miss.
- [ ] If pytest dominates, repeat Task 3 for the next measured hotspot. If synthetic acceptance dominates, profile the demo and repeated environment setup. Measure reuse of the synchronized environment by trying `uv run --offline --no-sync` after `uv sync --locked`. Keep distinct processes and the retention step's fresh absent data root. Retain only measured improvements that preserve all required checks.
- [ ] Collect at least five successful routine candidate job runs using the Task 1 measurement method. Prime any enabled cache before the timed warm runs and measure cold runs separately. Verify job logs show passing offline pytest and coverage, successful synthetic eval and offline evidence, and successful retention with the configured absent root. Inspect the upload step and artifact listing to confirm only `public-report.json` is uploaded.
- [ ] Compare medians using `1 - candidate_median_seconds / baseline_median_seconds`. Report run links, medians, cold-cache timings when applicable, test counts, line coverage, scoped mutation results, offline eval results, and known gaps. Claim the target only if the result is at least 80% and all gates pass. If the speed or test-count target falls short, report the measured result and remaining bottleneck before proposing a change of scope. Review the final diff and commit the verified CI optimization.
