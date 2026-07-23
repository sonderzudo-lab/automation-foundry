---
name: run-project-checkpoint
description: Audit Automation Foundry against its current roadmap, architecture, safety boundaries, tests, and definition of done, then produce an evidence-based readiness report with prioritized next actions. Use after a milestone, before a merge or release, when deciding what to build next, when returning after a pause, or when assessing whether a module is demo-ready, portfolio-ready, paper-trading-ready, or safe for scheduled local operation. Use read-only inspection unless the user also asks to fix the findings.
---

# Run a Project Checkpoint

Assess the repository as it exists, not as its documentation says it should exist.

## Set the checkpoint scope

1. Read `AGENTS.md`, architecture, roadmap, recent history, and relevant domain skills.
2. Identify the checkpoint target: repository, milestone, module, merge, demo, or operating mode.
3. Record the intended readiness level and its required evidence.
4. Preserve a read-only audit boundary unless implementation was explicitly requested.

## Collect evidence

Inspect and, when safe, run the relevant checks for:

- implemented code versus empty files, stubs, TODOs, mocks, and aspirational documentation;
- repository structure, configuration, migrations, and dependency reproducibility;
- unit, integration, smoke, and failure-path test results;
- run, step, approval, artifact, schedule, metric, ledger, and alert observability;
- idempotency, retries, timeouts, cancellation, kill switches, and recovery;
- secrets, logs, connector permissions, data retention, and licensing constraints;
- local-only network binding and absence of accidental public exposure;
- CPU, IO, and single-concurrency GPU queue separation;
- human gates before outbound, publishing, spending, destructive, or financial actions;
- roadmap status, documentation drift, and unresolved decisions;
- attributable cost, revenue, quality, and business-value measurement.

Never infer that a check passed because a configuration file or placeholder exists. Capture the command, test, code path, or artifact that supports each material conclusion.

## Classify findings

Assign every finding one status:

- **Verified:** direct evidence supports the requirement.
- **Partial:** some implementation or evidence exists, but a material gap remains.
- **Missing:** the requirement is absent or only aspirational.
- **Blocked:** verification requires unavailable hardware, credentials, live accounts, user choice, or external state.
- **Not applicable:** the requirement does not belong to the checkpoint scope.

Rank actionable gaps:

- **P0:** risks money, secrets, irreversible actions, or data integrity.
- **P1:** blocks the milestone or its primary user-visible path.
- **P2:** materially weakens reliability, observability, or maintainability.
- **P3:** useful improvement that does not block the target.

## Report the checkpoint

Lead with a one-sentence readiness verdict. Then provide:

1. scope and target readiness level;
2. verified capabilities with concise evidence;
3. prioritized gaps with file or system references;
4. checks run and their exact outcomes;
5. deferred home-PC, GPU, credential, or manual checks;
6. the smallest next vertical slice that improves readiness most;
7. a clear go, conditional go, or no-go recommendation for the target.

Do not convert uncertain evidence into a pass. Do not claim production, profitability, live-trading, or platform-policy readiness from unit tests alone.

## Readiness gates

For scheduled local operation, require safe restart behavior, bounded retries, observable failures, a kill switch, and a tested manual recovery path.

For portfolio readiness, require a reproducible setup, a coherent demo path, representative tests, visible metrics, honest limitations, and documentation matching the implementation.

For Trading Lab paper execution, additionally require bias-aware backtests, walk-forward validation, realistic fees and slippage, position and loss limits, stale-data detection, reconciliation, and real-money execution disabled by default.

For any live or irreversible operation, stop at a no-go unless the explicit human approval and safety requirements in the relevant domain skill are verified.
