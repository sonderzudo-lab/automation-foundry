---
name: operate-automation-foundry
description: Govern architecture, roadmap, cross-module changes, and local operation of Automation Foundry. Use for project-wide planning, repository restructuring, adding a new automation domain, changing shared contracts, coordinating control-plane and worker behavior, or reviewing whether a proposal fits the local-first product vision. Do not use for a narrow Content Engine, control-plane, or Trading Lab implementation when the corresponding domain skill is sufficient.
---

# Operate Automation Foundry

Treat Automation Foundry as a local-first platform that runs independent automations through one control plane.

## Establish context

1. Read the root `AGENTS.md` and the relevant architecture or roadmap documents before changing code.
2. Inspect the current implementation. Do not assume placeholder files are implemented.
3. Classify the task as platform, Content Engine, Trading Lab, or a new automation domain.
4. Load the narrower domain skill when one applies.
5. Preserve user changes and keep each change reviewable.

## Preserve product boundaries

- Run the application locally on the owner's home PC. Do not introduce public hosting, cloud deployment, multi-tenant auth, Kubernetes, or remote administration without an explicit scope change.
- Keep GitHub as source control, not the application runtime.
- Use the database as the source of truth for runs, steps, approvals, metrics, costs, alerts, and schedules.
- Keep domain tables and logic inside their automation module; place only reusable contracts in the platform layer.
- Require human approval before publishing, sending outbound messages, spending money, or enabling financial execution.
- Keep real-money trading disabled by default and isolated from every other automation.
- Route GPU work through a single-concurrency GPU queue. Keep CPU and IO work in separate queues.
- Never add AI attribution, `Co-authored-by`, `Made-with`, or `Generated-with` trailers to commits or pull requests unless the user explicitly requests them.

## Change workflow

1. State the desired outcome and affected modules.
2. Identify the smallest vertical slice that produces observable value.
3. Define inputs, outputs, state transitions, failure behavior, approval gates, metrics, and cost attribution.
4. Prefer a modular monorepo over premature microservices.
5. Implement one coherent slice at a time.
6. Add or update tests alongside code.
7. Update architecture and roadmap documents when contracts or priorities change.
8. Report what was verified and what requires the home PC, GPU, credentials, or manual review.

## New automation contract

Require every automation to declare:

- stable slug and owner;
- input and output schemas;
- schedule or trigger;
- ordered steps and queue class;
- idempotency key and retry policy;
- approval gates and kill switch;
- artifacts and retention policy;
- operational, financial, and quality metrics;
- required secrets and external services;
- health checks and failure alerts.

Do not create a separate area skill until its workflow and safety boundaries have become stable and repeatedly used.

## Definition of done

A change is complete only when its contracts are explicit, state is observable in the control plane, failure behavior is safe, relevant tests pass, secrets are not committed, and any hardware-dependent verification is clearly deferred rather than implied.

