---
name: build-control-plane
description: Build or review Automation Foundry's local control plane, including FastAPI and HTMX dashboard work, PostgreSQL or SQLite persistence, Redis and Celery orchestration, schedules, run tracking, approvals, metrics, alerts, and local machine health. Use for changes under shared core, dashboard, task, scheduler, observability, or infrastructure code. Do not use for domain pipeline logic except where it integrates through a shared contract.
---

# Build the Control Plane

Build one local control plane for all automation modules.

## Read first

Read `AGENTS.md`, the current architecture, shared models, configuration, database setup, and affected task or dashboard files. If the task changes a project-wide contract, also use `$operate-automation-foundry`.

## Core rules

- Bind the dashboard to loopback by default. Do not expose it publicly.
- Keep the database as the authoritative state store; Redis is a broker and cache, not durable truth.
- Use Celery for execution and Celery Beat for initial scheduling. Defer n8n until cross-application orchestration is justified.
- Separate `gpu`, `cpu`, and `io` queues. Set GPU worker concurrency to one.
- Make tasks idempotent and assign stable idempotency keys.
- Configure bounded retries, exponential backoff, soft and hard time limits, and structured errors.
- Record every run and step transition: queued, running, succeeded, failed, cancelled, or awaiting approval.
- Store secrets outside the database when possible; otherwise encrypt them before persistence.
- Prefer PostgreSQL once concurrent workers are enabled. Keep SQLite only for early single-process development and tests.

## Shared data model

Keep generic concepts in the platform layer:

- Automation
- Run
- StepRun
- Approval
- Artifact
- Schedule
- ConnectorObservation
- MetricPoint
- Experiment
- LedgerEntry
- Alert

Do not move Content Engine or Trading Lab entities into shared tables merely to reuse code.

## Dashboard requirements

Make these states visible:

- current and next runs;
- success rate and duration;
- pending approvals and their age;
- failed steps and retry count;
- revenue, cost, and net result when attributable;
- GPU, CPU, memory, disk, Redis, and worker health;
- data freshness and connector status.

Prefer server-rendered HTMX and Jinja for the initial local dashboard. Do not introduce a separate SPA unless the interaction model demonstrates a real need.

## Verification

Add unit tests for state transitions and scheduling, integration tests for database and queue boundaries, and one local smoke path. Never claim GPU, Docker, Windows service, or boot-time behavior was verified unless it ran on the home PC.

