# CareThread MVP — Engineering Specification Sheet

**Date:** 2026-11-24  
**Status:** Approved L200 Design -> Ready for Implementation Planning  
**Source Documents:**
- [01-high-level-architecture.md](file:///usr/local/google/home/sabaridosapati/Projects/HTMAgent/plan/01-high-level-architecture.md)
- [02-low-level-system-design.md](file:///usr/local/google/home/sabaridosapati/Projects/HTMAgent/plan/02-low-level-system-design.md)
- [03-agentops-coverage-and-evaluations.md](file:///usr/local/google/home/sabaridosapati/Projects/HTMAgent/plan/03-agentops-coverage-and-evaluations.md)
- [04-l200-reviewer-walkthrough.md](file:///usr/local/google/home/sabaridosapati/Projects/HTMAgent/plan/04-l200-reviewer-walkthrough.md)

---

## 1. Purpose & MVP Scope

Build the **CareThread** Google-only clinical documentation and longitudinal continuity assistant as a testable, modular Python + ADK codebase with Terraform IaC and CI evaluation gates, using **100% synthetic fixtures (zero real PHI)**.

### Three Core Workflows
1. **Jan 31 Neurology Visit (`ENC-2026-0131-NEURO`):** Ingest short two-person synthetic audio -> verbatim transcription with diarization & word timestamps (`gemini-3.5-transcribe` `VERBATIM`, fallback `chirp_3` `BatchRecognize`) -> ADK sequential SOAP drafting (`gemini-3.8-flash`, schema-enforced) -> deterministic evidence verification + bounded regeneration (max 2 retries; optional eval-gated `gemini-4-argon` max 1 escalation) -> durable digest-bound clinician HITL sign-off -> deterministic FHIR R4 commit (`Composition`, `Condition`, `Observation`, `MedicationStatement`/`MedicationRequest`, `Provenance`) -> async Cloud Tasks Spanner Graph refresh.
2. **Mar 31 Orthopedic Continuity (`ENC-2026-0331-ORTHO`):** Deterministic tenant + care-relationship + active-consent check -> bounded patient-first Spanner Graph traversal (`max_hops=2`, `max_encounters<=25`) -> FHIR R4 `meta.versionId` freshness verification -> automatic authoritative FHIR R4 fallback on staleness/absence/timeout -> continuity brief keeping Jan 31 history dated, attributed, evidence-linked, and structurally separated from Mar 31 assessment.
3. **Synthetic Insurer Release:** Expiring purpose-bound grant (`purpose=prior_auth_documentation`, default TTL 72h) + explicit patient authorization + durable digest-bound human release decision -> allowlisted signed-record packet only + deterministic documentation coverage checklist (`numerator`, `denominator`, `missing_items`). Strictly prohibits health risk scoring, claims adjudication, eligibility decisions, premiums, and underwriting.

---

## 2. Hard Invariants (Binding Across All Components)

1. **Source of Truth:** Cloud Healthcare API **FHIR R4** (`enableResourceVersioning = true`) is the sole signed clinical record. **Spanner Graph** is a derived, eventually consistent continuity index—never the system of record and never the authorization boundary.
2. **Deterministic Boundaries:** Authentication, tenant isolation, consent/grant validation, FHIR R4 writes, HITL digest verification, and insurer packet allowlist filtering execute strictly in deterministic Python services/workers—never via LLM-callable write tools.
3. **Medication Semantics:** Every medication item uses one of exactly six statuses: `discussed`, `planned`, `prescribed`, `reported`, `stopped`, `unclear`. A `discussed`, `planned`, `reported`, `stopped`, or `unclear` medication **NEVER** creates a FHIR `MedicationRequest` and **NEVER** implies active current use.
4. **HITL Digest Binding:** Every clinical sign-off and insurer packet release binds to `(artifact_id, artifact_version, sha256_digest)`. Editing an artifact changes its digest and invalidates prior approvals (`INVALIDATED_SUPERSEDED`). Expired or replayed approvals fail closed.
5. **Zero-PHI Routine Telemetry:** Raw prompts, patient identifiers, transcripts, model outputs, and GCS audio object paths never enter Cloud Logging, Cloud Trace attributes, or Firestore ADK session state. Allowlisted metadata only; residual permitted free text is actively scrubbed via Sensitive Data Protection (`content.deidentify`), failing closed (drop/quarantine free text + increment `sdp_unavailable_fail_closed`) when SDP is unavailable.
6. **Least-Privilege Service Identities & Zero Hardcoded Secrets:** Dedicated GCP service accounts per service (`sa-api`, `sa-adk-runtime`, `sa-worker-transcribe`, `sa-worker-fhir`, `sa-worker-graph`, `sa-worker-release`) via ADC; ADK runtime has zero FHIR write and zero raw-audio read permissions. External/signing secrets resolve exclusively via Secret Manager.
7. **Schema & Evidence Hard Gate:** All contracts use `schema_version = "1.0.0"`. Missing clinical fields emit explicit `null` + `missing_reason`. Every clinical claim cites verified `evidence_id`(s).

---

## 3. Repository & Module Architecture

```text
HTMAgent/
├── pyproject.toml                         # Python 3.12+/3.14, uv, pydantic, pytest, opentelemetry, google-adk, google-genai
├── cloudbuild.yaml                        # CI: lint, secret-scan, pytest golden suite (hard gates), terraform validate
├── docs/
│   ├── build-with-agents-cli.md           # Google Agents CLI (agents-cli) & adk eval/run build evidence
│   └── superpowers/
│       ├── specs/2026-11-24-carethread-mvp-spec.md
│       └── plans/2026-11-24-carethread-mvp-plan.md
├── infra/terraform/
│   ├── main.tf                            # Root module wiring
│   ├── variables.tf / outputs.tf
│   └── modules/
│       ├── iam/                           # 6 least-privilege service accounts + ADC bindings
│       ├── storage/                       # Private CMEK GCS buckets + lifecycle rules
│       ├── healthcare_fhir/               # Cloud Healthcare API FHIR R4 dataset & versioned store
│       ├── spanner_graph/                 # Spanner Enterprise instance + DDL + CREATE PROPERTY GRAPH
│       ├── firestore/                     # Native Firestore + TTL policies
│       ├── tasks_and_run/                 # Cloud Run services + Cloud Tasks queues
│       └── security_obs/                  # Identity Platform, SDP templates, Secret Manager, Logging/Trace
├── src/carethread/
│   ├── __init__.py
│   ├── schemas/v1/                        # Versioned Pydantic v2 contracts (SOAP, Continuity, Packet, Transcript, Jobs)
│   ├── errors.py                          # GuidedErrorEnvelope (v1.0.0) & error categories
│   ├── observability/
│   │   ├── logger.py                      # Structured JSON logger (allowlist-only, bans print)
│   │   ├── redaction.py                   # SDP client protocol + fail-closed scrubber
│   │   └── tracing.py                     # OpenTelemetry W3C traceparent propagation
│   ├── security/
│   │   ├── authz.py                       # Deterministic tenant/role/care-relationship verifier + ScopeCapabilityToken
│   │   ├── consent.py                     # Patient consent & expiring purpose-bound grant verifier
│   │   └── secrets.py                     # Secret Manager adapter (no hardcoded keys)
│   ├── stores/
│   │   ├── gcs.py                         # Private CMEK audio/transcript/packet store protocol + local fake
│   │   ├── fhir.py                        # FHIR R4 authoritative store + conditional writes + Provenance + reconciler
│   │   ├── spanner_graph.py               # Spanner Graph derived index + bounded patient-first GQL traversal
│   │   └── outbox.py                      # Firestore job state machine, leases, idempotency & transactional outbox
│   ├── session/
│   │   └── firestore_session.py           # Filtered ADK session store (zero PHI) + bounded compaction (6 turns / 16k tokens)
│   ├── hitl/
│   │   └── confirmations.py               # Durable digest-bound HITL approval store (sign-off & release)
│   ├── prompts/
│   │   └── constitution_v1.py             # Clinical Documentation Constitution + untrusted data delimiters
│   ├── routing/
│   │   └── model_router.py                # Model routing (3.5-transcribe/chirp_3, 3.8-flash, eval-gated 4-argon)
│   ├── plugins/
│   │   └── policy_plugin.py               # ADK BasePlugin-compatible policy gate + INTENT/OUTCOME logging
│   ├── tools/
│   │   ├── registry.py                    # Strict <verb>_<domain_noun> tool registry & docstring validator
│   │   └── clinical_tools.py              # All 8 versioned ADK tools (read/validate/HITL only — zero FHIR writes)
│   ├── services/
│   │   ├── continuity.py                  # Spanner Graph retrieval + FHIR version verification + authoritative fallback
│   │   └── insurer_release.py             # Allowlisted packet assembler + deterministic coverage checklist
│   └── agents/
│       └── coordinator.py                 # Deterministic ADK Coordinator + Sequential VisitDraft pipeline (bounded retries)
├── evals/
│   ├── fixtures/                          # 100% fabricated Jan 31 / Mar 31 / Insurer synthetic fixtures
│   └── test_golden_suite.py               # Automated hard-gated 10 Golden Cases (GC-01 through GC-10)
└── tests/                                 # Per-module unit & integration test suites
```

---

## 4. Core Interface & Data Contracts (`v1.0.0`)

### 4.1 Tool Surface (8 ADK Tools — Descriptive Naming & Caller Allowlists)
1. `fetch_verbatim_transcript_segments` — Callers: `soap_extractor_agent`, `evidence_verifier_agent`
2. `validate_soap_draft_grounding` — Callers: `evidence_verifier_agent`, `adk_coordinator`
3. `request_clinician_signoff_confirmation` — Callers: `adk_coordinator`
4. `query_patient_continuity_graph` — Callers: `continuity_retrieval_agent`
5. `verify_fhir_resource_versions` — Callers: `continuity_retrieval_agent`, `evidence_verifier_agent`
6. `fetch_authoritative_fhir_history` — Callers: `continuity_retrieval_agent`
7. `assemble_allowlisted_insurer_packet` — Callers: `records_release_coordinator`
8. `request_release_hitl_confirmation` — Callers: `records_release_coordinator`

### 4.2 Guided Error Categories
`AUTHZ_DENIED`, `CONSENT_REVOKED`, `STALE_GRAPH_DETECTED`, `VALIDATION_RECOVERABLE`, `HITL_DIGEST_MISMATCH`, `SDP_UNAVAILABLE_FAIL_CLOSED`, `GRANT_EXPIRED`, `OUT_OF_SCOPE_PURPOSE`.

### 4.3 Job State Machine
`QUEUED` -> `TRANSCRIBING` <-> `FAILED_RETRYABLE` -> `TRANSCRIBED` -> `DRAFTING` -> `AWAITING_CLINICIAN_REVIEW` -> (`REJECTED_BY_CLINICIAN` -> `DRAFTING` bounded | `COMMITTING_FHIR` -> `RECONCILING_FHIR` <-> `COMMITTING_FHIR` -> `SIGNED_COMMITTED`) | `FAILED_TERMINAL`.

### 4.4 AgentOps Rubric & Golden Cases Coverage
- All **19 criteria (95 points)** from [03-agentops-coverage-and-evaluations.md](file:///usr/local/google/home/sabaridosapati/Projects/HTMAgent/plan/03-agentops-coverage-and-evaluations.md) map directly to modules and CI checks.
- All **10 Golden Cases (`GC-01`–`GC-10`)** execute offline in `pytest` via deterministic adapters and serve as CI hard gates.
