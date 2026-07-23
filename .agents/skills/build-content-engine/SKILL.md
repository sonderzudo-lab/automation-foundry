---
name: build-content-engine
description: Build or review the Content Engine automation inside Automation Foundry, including topic research, script generation, TTS, visuals, captions, FFmpeg assembly, originality checks, human review, private upload, analytics, cost attribution, and monetization experiments. Use for files in content pipeline, intelligence, engagement, revenue, SEO, repurposing, or channel operations. Preserve commercial-use licensing and platform-policy safeguards.
---

# Build the Content Engine

Treat the Content Engine as one automation domain, not the platform itself.

## Read first

Read `AGENTS.md`, the Content Engine sections of `ARQUITETURA.md`, affected implementation files, and their tests. Preserve the implemented A1 script-generation behavior unless the task explicitly changes its contract.

## Pipeline contract

Keep the pipeline explicit and restartable:

1. topic and source selection;
2. editorial angle approval;
3. script and narration generation;
4. TTS;
5. visual generation or licensed retrieval;
6. captions;
7. FFmpeg assembly;
8. similarity and originality checks;
9. human approval;
10. private upload and later publication;
11. analytics, revenue, and cost collection.

Persist each step as an observable job. Store artifacts under a stable channel and content identifier. Make regeneration deliberate so retries do not silently replace approved work.

## Safety and quality gates

- Never publish without human approval of topic, angle, script, thumbnail, and final render.
- Never automate comments or outbound engagement without approval.
- Block or escalate content above the configured similarity threshold.
- Avoid generic templates and mass-produced variations. Preserve an authentic editorial contribution.
- Track source provenance and commercial-use rights for visuals, audio, models, and datasets.
- Use only commercially permitted model variants documented by the project.
- Upload as private during the initial workflow.
- Keep credentials scoped per platform and never store raw OAuth tokens without encryption.

## Implementation rules

- Put GPU operations on the single-concurrency GPU queue, FFmpeg on CPU, and APIs or uploads on IO.
- Validate output shape, language, duration, and empty responses instead of trusting prompts alone.
- Make filesystem writes atomic where practical.
- Record duration, retries, model version, energy estimate, API cost, and final outcome.
- Mock external services in unit tests and add a small opt-in integration test for local Ollama or media tools.

## Definition of done

A module is done only when its artifact can be reviewed, its job state is visible, retries are safe, tests cover the failure path, licensing assumptions are documented, and no automatic publication bypasses approval.

