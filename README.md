# CareThread — Clinical Documentation & Continuity Assistant (MVP)

**CareThread** is a Google-managed clinical documentation, longitudinal continuity, and purpose-bound insurer release assistant built with **Google ADK** patterns, **Cloud Healthcare API (FHIR R4)** as the sole signed clinical record, **Spanner Graph** as a derived continuity index, **Sensitive Data Protection (SDP)** fail-closed telemetry redaction, and **Terraform** Infrastructure as Code.

> **Synthetic Fixtures Only:** This repository uses 100% fabricated patient, clinician, and insurer fixtures (zero real PHI).

## Architecture & Design Documents

- **L200 Proposal (`plan/`):**
  - [01-high-level-architecture.md](plan/01-high-level-architecture.md)
  - [02-low-level-system-design.md](plan/02-low-level-system-design.md)
  - [03-agentops-coverage-and-evaluations.md](plan/03-agentops-coverage-and-evaluations.md)
  - [04-l200-reviewer-walkthrough.md](plan/04-l200-reviewer-walkthrough.md)
- **Engineering Spec & Implementation Plan (`docs/superpowers/`):**
  - [CareThread MVP Spec Sheet](docs/superpowers/specs/2026-11-24-carethread-mvp-spec.md)
  - [CareThread MVP Implementation Plan](docs/superpowers/plans/2026-11-24-carethread-mvp-plan.md)

## Core Invariants

1. **Authoritative Source of Truth:** Cloud Healthcare API **FHIR R4** (`enableResourceVersioning = true`) is the sole signed clinical store; **Spanner Graph** is a derived, eventually consistent continuity index verified against FHIR `meta.versionId` on every read with automatic FHIR fallback.
2. **Deterministic Security & Write Boundaries:** Authentication, tenant isolation, consent/grant checks, FHIR R4 writes, HITL digest verification, and insurer allowlist filtering run exclusively in deterministic services—never via LLM-callable write tools.
3. **Medication Status Discipline:** Six explicit statuses (`discussed`, `planned`, `prescribed`, `reported`, `stopped`, `unclear`). A `discussed` medication never creates a FHIR `MedicationRequest` or implies active current use.
4. **Durable Digest-Bound HITL Gates:** Clinician sign-off and insurer packet release bind to exact `(artifact_id, artifact_version, sha256_digest)`.
5. **Zero-PHI Routine Telemetry:** Allowlist-first JSON logging and OpenTelemetry tracing with active SDP scrubbing on residual free text (failing closed when SDP is unavailable).

## Quickstart (Local Verification)

```bash
# Run unit tests and the 10-case hard-gated Golden Evaluation Suite
.venv/bin/pytest tests/ evals/ -v
```

## Project Layout

- `src/carethread/` — Schemas (`v1.0.0`), observability/redaction, security/authz/consent, GCS/FHIR/Spanner/outbox stores, filtered session & HITL confirmations, model router, constitution, ADK policy plugin, 8 versioned tools, and deterministic coordinator.
- `evals/` — Fabricated synthetic fixtures and the 10 hard-gated golden cases (`GC-01` through `GC-10`).
- `infra/terraform/` — Modular Google-provider Terraform IaC (6 least-privilege service accounts, CMEK GCS, FHIR R4, Spanner Graph DDL, Firestore, Cloud Run/Tasks, SDP, Secret Manager).
- `cloudbuild.yaml` — CI pipeline enforcing lint, secret scanning, golden evaluation hard gates, and `terraform validate`.
