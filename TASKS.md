# Foundation implementation ledger

Plan: docs/superpowers/plans/2026-11-09-foundations.md
Branch: codex/finance-foundations
Worktree: managed attachment for this chat; local absolute path is in the ignored Superpowers workspace.

| Task | Status | Owner | Tests | Evals / trace | Gaps |
| --- | --- | --- | --- | --- | --- |
| 1 | complete | Codex; independent spec + quality reviewers | initial missing-app RED; review regressions 4 + 3 RED; full suite 29 passed | N/A until Tasks 5/7 | no agents/state/traces/eval harness yet |
| 2 | complete | Codex; independent spec + quality reviewers | missing-module RED; verdict7RED + duplicate2RED; evidence26 / combined68 GREEN | N/A until Tasks 5/7 | semantic entailment remains future agent/human work |
| 3 | complete | scoped implementer; independent spec + quality reviewers | storage14 / full69 GREEN; durability and REPLACE regressions RED→GREEN | N/A persistence-only; Tasks 5/7 pending | filesystem fsync and trusted-machine assumptions documented |
| 4 | complete | scoped implementer; separate spec + Astra medium quality reviews | staged RED→GREEN; full314 GREEN | N/A until Tasks 5/7 | P3 independent matrix oracle deferred; auth/production validators absent/failclosed |
| 5 | complete | native completion; separate spec + Astra medium quality reviews | privacy/timeout/timestamp RED→GREEN; full348 GREEN | actual SDK two-project offline bundle verified; live Phoenix Task9 | at-least-once; timeout health may be process-local; no agent eval |
| 6 | complete | scoped runner implementer; separate spec + quality reviews | initial2RED; serialization10RED→GREEN; focused35/full383 GREEN | synthetic offline3span trace verified; no editorial quality claim | single stopped-runner recovery; uncertain external work interrupted |
| 7 | complete | scoped dataset implementer; separate spec + quality reviews | missing-loader22RED; expectation2RED→GREEN; dataset31/full414 GREEN | immutable1.0.0;3cases/split;2criticalrejections/split | syntheticonly; suppliedfingerprints; no actualagent eval |
| 8 | complete | scoped implementers; separate spec + Astra quality reviews | staged RED→GREEN; focused62/full476 GREEN | all3 synthetic splits PASS3cases/18checks; exits0/1/2; offline trace join | fixturebaseline not human-approved; no real judge quality |
| 9 | complete | scoped adapter; separate spec + Astra quality reviews; root live/UI | privacy/atomic-export RED→GREEN; full506 GREEN1skip; live1 GREEN | actual datasets/experiments/scores/UI; replay2→2; verified self-contained offline export | Phoenix21 single trace owner; later topology/index follow-up; ambiguous writes need reconciliation |
| 10 | complete | scoped implementer; separate spec + Astra quality reviews | staged RED→GREEN; release71/full577 GREEN1skip | 3splits x2repeats PASS;42offline spans; regular/offline/mixed await bootstrap | default-deny verifier until Tasks11/12; no real production |
| 11 | complete | scoped auth implementer; separate spec + Astra quality reviews | auth36/full613 GREEN1skip; callback telemetry6→0 | all3 synthetic eval splits PASS; DB/log/trace sentinel checks | live GitHub OAuth unrun; local trusted-machine boundary |
| 12 | complete | scoped review implementer; separate spec + Astra quality reviews | RED→GREEN; review74/full687 GREEN1skip | all3synthetic splits x2 PASS; tested offline fallback | live OAuth unrun; fresh-schema hardening; minor Services positional compatibility deferred |
| 13 | complete | scoped implementer; separate spec + Astra quality reviews | RED→GREEN; monitoring22/full709 GREEN1skip | all3synthetic splits PASS; verified offline bundles/index | pending-only telemetry; unknown Phoenix owner; test clock minor deferred |
| 14 | complete | scoped implementer; separate spec + Astra quality reviews | RED→GREEN; retention22/full731 GREEN1skip | all3synthetic splits PASS; offline2 pass; fresh absent-root dryrun exit0 | active WAL fails closed; pending/offline exports protected; incomplete-fixture minor deferred |
| 15 | complete | scoped implementer; separate spec + Astra quality reviews | RED→GREEN; E2E10/full741 GREEN1live-deselected | fresh offline demo PASS/AWAITING_APPROVAL;3split bundles/10execution spans verified | remoteCI/liveOAuth/currentPhoenix unrun; final review10findingsresolved; legacy1.0registry migrationpending |

Final whole-branch acceptance2026-11-16:754offlinepassed/1live-deselected; single finalfixwave and scopedreview addressedall10findings. Demo1.1 PASS/AWAITING_APPROVAL;54evaluator+10execution spansverified. Compatibility limits: approved1.0registry needsgovernedmigration/newexplicitbootstrap; runbookDemoReportversionline stale. See docs/superpowers/reviews/2026-11-16-foundations-acceptance.md.
