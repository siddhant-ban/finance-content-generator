# CI test reduction with mutmut

## Goals

Reduce median end-to-end wall time for required PR CI by at least 80% and remove or consolidate redundant tests. Require at least 90% line coverage across `app/` from the required offline pytest suite, including modules that the tests never import. Preserve the offline synthetic acceptance run, retention dry run, sanitized report upload, and direct tests of approval, release, and other fail-closed behavior.

## Approaches

1. **Prune tests only.** Use mutmut to find overlap among slow tests. Savings are limited to the time pytest spends on the tests we can safely remove.
2. **Optimize the whole required job.** The user chose this approach. Profile dependency setup, tests, acceptance, and the rest of the job. Use mutmut to validate pruning and address other measured bottlenecks, such as repeated dependency downloads.
3. **Run a smaller required suite and defer broader checks until after merge.** This can shorten PR runs at the cost of discovering some regressions after merge.

## Measurement and rollout

1. **Baseline on Ubuntu.** Record job start-to-finish wall time and step durations for the current workflow as configured. Measure pytest collection, slow tests, test count, and line coverage in a diagnostic run. Use at least five successful timing runs per variant on comparable GitHub-hosted Ubuntu runners with the repository's pinned Python, locked dependencies, and identical application code. Record queue time separately. Compare the routine PR configuration for each variant. If the candidate enables caching, use a primed cache for its routine comparison and report its cold-cache timing separately. Include coverage collection overhead in the final workflow timing.
2. **Pilot on a measured hotspot.** Choose one slow group with evidence of overlapping tests. Run a pinned mutmut version on Linux against a source scope covering the behavior under review, with the original offline suite and then the proposed retained suite. Use the same generated mutant set in both runs. Check individual mutant results and preserve every baseline kill. Investigate timeouts, missing results, and tool errors before accepting the comparison. Keep direct tests for distinct contracts, failure modes, and safety boundaries even where mutation results overlap. If coverage is below 90%, close gaps with narrowly targeted tests and measure the net change in collected cases. Retain pruning changes only if they also improve measured runtime.
3. **Optimize the bottleneck.** If setup dominates, measure uv caching with lockfile-based invalidation. If pytest dominates, expand the pruning pilot and measure further changes individually. If acceptance dominates, profile repeated setup and reuse work only where the existing acceptance and retention checks still exercise their intended conditions. Keep mutation runs on demand and outside the required PR job.
4. **Verify the final job.** The offline suite must pass at 90% or higher line coverage, and the offline acceptance and retention steps must pass. Compare median job times and report `1 - final median / baseline median`. Report test counts, coverage, mutation deltas for the pruned scope, cold-cache timings, and offline eval results. If measurements show that the 80% target is out of reach with these constraints, report the measured reduction and remaining bottleneck before proposing a scope change.

## Constraints and open measurements

Current CI runs on Ubuntu, installs locked dependencies with uv caching disabled, and has no enforced coverage floor. Native Windows pytest collection currently fails on a Unix-only `fcntl` import. Mutmut also needs a fork-capable environment. Use Linux for baseline and mutation evidence. The current Ubuntu runtime breakdown, coverage, and mutation baseline remain to be measured. This design covers CI and tests. Application behavior, external services, and recurring spend are outside its scope.
