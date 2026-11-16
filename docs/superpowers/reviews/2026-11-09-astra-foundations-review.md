# Independent foundation plan review

Date: 2026-11-09.
Reviewer: independent subagent using `gpt-6-astra`, reasoning effort `medium`,
explicitly requested by the user. Review used a fresh context, not the author's
conversation history. The same reviewer performed the follow-up check.

Reviewed: [approved design](../specs/2026-11-09-foundations-design.md) and
[implementation plan](../plans/2026-11-09-foundations.md), against `AGENTS.md`,
architecture, evaluation playbook, observability documentation and original roadmap.

## Initial assessment

Not ready as written: two localized P2 findings. No P0/P1 safety bypass identified.

1. **Early evidence-gap recovery:** entry to `NEEDS_RESEARCH` was allowed from
   `DISCOVERED` and `CLUSTERED`, but the only recovery target required an existing
   eligible score. Early stories could not recover after correction.
2. **Mutation idempotency:** release-approval service/browser interfaces omitted
   the key required by the design. Creation also lacked an explicit replay rule.
   A lost response could lead to conflicting retries or duplicate decision events.

## Verified plan corrections

- `NEEDS_RESEARCH` resumes only the saved valid `DISCOVERED`, `CLUSTERED`, or
  `SCORED` checkpoint. Corrected inputs are revalidated; later dispatch still
  requires an eligible ADVANCE decision. Task 4 now requires early-stage recovery
  and no-bypass tests.
- Creation now defines caller scope, idempotency key, absent-story expected
  version, immutable request fingerprint and original-response replay. Changed
  payload reuse conflicts; an existing story cannot be recreated with another key.
- Both release-approval service layers and browser forms now carry a stable key.
  Live authentication is required before replay lookup. Identical retry returns
  the original receipt without another mutation/event; changed payload conflicts;
  replay never reactivates a revoked release. Tasks 10/12 require regression tests.

## Independent follow-up assessment

> Both findings are resolved. The plan is ready for implementation, starting with
> Task 1. No remaining important blocker found.

The reviewer confirmed dependency compatibility and live OAuth/Phoenix evidence
are assigned execution checks rather than unresolved planning blockers. Synthetic
foundation evaluation remains distinct from real-agent quality and approval.

## Scope and verification

Files changed by the reviewer: none. The author updated only the plan and this
review record. No application implementation, agent, dependency installation,
service startup, tests or agent evaluations were performed. Local document checks
verified the corrected recovery rule, idempotency signatures/test requirements,
links and whitespace; those checks are not application test evidence.
