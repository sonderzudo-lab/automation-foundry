---
name: build-automation-module
description: Add or substantially extend an Automation Foundry automation module as a safe, observable vertical slice. Use when introducing a new automation domain, connector-backed workflow, scheduled job, or pipeline that must integrate with shared runs, steps, queues, approvals, artifacts, metrics, costs, alerts, and the local dashboard. Do not use for changes confined to an existing Content Engine, Trading Lab, or control-plane workflow when its narrower skill is sufficient.
---

# Build an Automation Module

Create modules that can run independently while obeying the platform contract.

## Establish the boundary

1. Read `AGENTS.md`, the current architecture and roadmap, and `$operate-automation-foundry`.
2. Inspect real code and tests. Distinguish implemented behavior from placeholders and design documents.
3. Define one user-visible outcome for the first vertical slice.
4. Decide whether the capability belongs in an existing module before creating a new one.
5. Name the module with a stable lowercase slug that describes its domain rather than a temporary implementation.

## Specify the contract

Define before implementation:

- typed input and output schemas;
- manual, scheduled, event, or API trigger;
- ordered steps and their `gpu`, `cpu`, or `io` queue class;
- stable idempotency key and duplicate-handling behavior;
- retry limits, timeouts, cancellation, and recovery behavior;
- approval gates and a module-level kill switch;
- artifacts, storage location, retention, and cleanup policy;
- operational, quality, financial, and business metrics;
- cost and revenue attribution where measurable;
- secrets, external services, rate limits, and data freshness requirements;
- health checks, failure alerts, and degraded-mode behavior.

Reject a design that cannot explain what happens after partial failure or repeated delivery.

## Implement the vertical slice

1. Keep domain models, services, tasks, and adapters inside the module.
2. Reuse shared platform contracts for runs, step runs, approvals, schedules, artifacts, metrics, ledger entries, and alerts.
3. Treat the database as the source of truth and queues as delivery mechanisms.
4. Make external adapters replaceable and isolate their credentials and rate-limit logic.
5. Record state transitions and structured errors at each step.
6. Surface run status, pending approvals, failures, relevant metrics, and cost in the local dashboard.
7. Add a manual trigger before enabling a schedule.
8. Keep outbound messages, publishing, spending, destructive actions, and financial execution behind explicit human approval.

Do not make Codex, Claude, Cursor, an MCP server, or a plugin a runtime dependency. Implement product integrations as application adapters so Automation Foundry runs unattended without an AI coding agent.

## Verify behavior

- Unit-test schemas, state transitions, idempotency, and business rules.
- Test adapters with fakes or recorded fixtures; do not hit paid or irreversible services by default.
- Add integration coverage for persistence and queue boundaries where practical.
- Exercise one local smoke path from trigger to observable result.
- Test retry, duplicate delivery, partial failure, cancellation, and approval rejection.
- Verify secrets are absent from tracked files and logs.
- State explicitly which checks require the home PC, GPU, credentials, live accounts, or manual review.

## Finish the module

Update architecture and roadmap documents when the new module changes priorities or shared contracts. Do not create a dedicated area skill until the workflow has stabilized and been used repeatedly. Run `$run-project-checkpoint` at the end of a milestone or before proposing the module as portfolio-ready.
