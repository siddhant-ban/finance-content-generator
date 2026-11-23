# Evidence on greenfield coding workflows

Reviewed 2026-11-19. Research note only; no coding-agent experiment was run.

## Finding

The primary sources reviewed do not establish a statistically supported winner among Matt Pocock Skills, Addy Osmani Agent Skills, and Superpowers for whole-app success per token. This is a bounded search finding, not proof that no comparison exists.

## What the sources establish

- [Matt Pocock's repository](https://github.com/mattpocock/skills) describes small, adaptable, composable engineering skills rather than a framework that owns the process. Its README provides no controlled whole-app/token comparison.
- [Superpowers](https://github.com/obra/superpowers) describes a complete development methodology with composable skills. Process coverage is evidence of intended behavior, not evidence of superior cost-adjusted outcomes.
- [Addy's repository](https://github.com/addyosmani/agent-skills) covers the development lifecycle. Its [eval documentation](https://github.com/addyosmani/agent-skills/blob/main/evals/README.md) separates structural validation, lexical routing tests, and on-demand behavioral tests. These test skill behavior and selection; they do not constitute repeated whole-app outcome comparisons across the three packs.
- [Addy's releases](https://github.com/addyosmani/agent-skills/releases) report that removing redundant session-start routing saved approximately 3,500 tokens per session on hosts with native routing. This is a maintainer-reported overhead reduction, not a greenfield efficiency ranking.
- A [direct comparison repository](https://github.com/tgmarinho/agent-super-powers-matt-pocock-skills-comparasions) includes React implementation and tabletop tasks, but explicitly states that it is not statistically rigorous. Its [evidence file](https://github.com/tgmarinho/agent-super-powers-matt-pocock-skills-comparasions/blob/main/EVIDENCE.md) frames results as workflow guidance, not universal performance claims. The tasks do not establish whole-app production success per token.

## Relevant whole-app experiment

[Anthropic's harness research](https://www.anthropic.com/research/harness-design-long-running-apps) reports a game-maker example: a solo run cost $9 over 20 minutes, while a planner/generator/evaluator harness cost $200 over six hours. The latter delivered substantially more scope, while the solo result had broken gameplay. Scope and duration therefore confound any efficiency inference. This was not a comparison of the three skill packs.

The same research reports that switching from Sonnet 4.5 to Opus 4.5 allowed removal of explicit context resets. This supports adapting orchestration to the model and observed failure modes rather than assuming more process always helps.

## Practical inference, not a benchmark result

Choose one workflow backbone. A hands-on developer can start with focused composable skills; stronger process enforcement or broader production checks may justify additional overhead. Measure total billed cost, retries, elapsed time, human intervention, and independently verified acceptance criteria. Token totals alone do not capture cache pricing, model differences, or delivered scope.

## Validation and limitations

- Files changed: this research note only.
- Tests: not run; no executable changes.
- Evals: no local evals run; source evaluation scope reviewed above.
- Known gaps: no repeated, fixed-scope, fixed-model greenfield benchmark across all three packs was found. Source repositories change; the linked main branches are not pinned snapshots.
