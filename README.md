# CareThread — Clinical Documentation & Continuity Assistant (MVP)

A Google-only clinical documentation, longitudinal continuity, and purpose-bound insurer release assistant built with **Google Agent Development Kit (ADK)** patterns in Python, **Cloud Healthcare API (FHIR R4)** as the sole signed clinical record, **Spanner Graph** as a derived continuity index, **Sensitive Data Protection (SDP)** fail-closed telemetry redaction, and **Strategic Model Routing** (`gemini-3.5-transcribe` / `chirp_3`, `gemini-3.8-flash`, and optional eval-gated `gemini-4-argon`) on Vertex AI.

> **Synthetic Fixtures Only:** All patient, clinician, encounter, and insurer data in this repository use 100% fabricated fixtures (zero real PHI).

---

## 🎯 Purpose of the Project

Unstructured clinical conversations capture critical longitudinal context—chief complaints, functional limits, stated exam findings, medication discussions that are *not* yet prescriptions, lifestyle guidance, and follow-up plans—that is routinely lost or distorted across specialty handoffs.

**CareThread** solves this across three tightly bounded workflows:

1. **Evidence-Linked Visit Documentation (31 January — Neurology):** Ingests a short two-person synthetic clinic recording into private CMEK-encrypted Cloud Storage, transcribes it verbatim with reviewable speaker diarization and word timestamps (`gemini-3.5-transcribe` in `VERBATIM` mode, fallback `chirp_3` `BatchRecognize`), and runs a deterministic ADK **Sequential Extraction -> Evidence Verification -> Optional Conflict Review** pipeline (`gemini-3.8-flash` with strict `response_schema`) to produce an evidence-linked SOAP draft. A clinician verifies speaker roles and signs the exact `(draft_version, sha256_digest)` before deterministic backend workers commit the signed record to **Cloud Healthcare API (FHIR R4)** and asynchronously refresh **Spanner Graph**.
2. **Longitudinal Continuity Retrieval (31 March — Orthopedics):** After deterministic server-side tenant, care-relationship, and active-consent verification, an authorized orthopedic clinician views dated, attributed January history from a bounded patient-first **Spanner Graph** traversal verified against current FHIR R4 `meta.versionId` values—with automatic **authoritative FHIR R4 fallback** on staleness, absence, or timeout. Prior statements stay structurally distinct from the March assessment, and a medication *discussed* in January never establishes active current use.
3. **Purpose-Bound Insurer Release (Synthetic Demo):** Under a time-bounded, expiring purpose-bound grant (`prior_auth_documentation`) with explicit patient authorization and a durable digest-bound human release decision, CareThread assembles an **allowlisted signed-record packet** plus a deterministic **documentation coverage checklist** (`numerator`, `denominator`, `missing_items`). Individual health scoring, claims adjudication, eligibility decisions, premiums, and underwriting are strictly prohibited by architecture and policy.

---

## 🏗️ Architecture & Modular Design

CareThread follows a clean hexagonal architecture separating deterministic security, storage, FHIR R4, and graph services from ADK orchestration and tool contracts:

*   `src/carethread/schemas/v1/`: Strict versioned (`1.0.0`, `extra="forbid"`) Pydantic v2 contracts for SOAP drafts (`soap.py`), continuity briefs (`continuity.py`), allowlisted insurer packets & coverage checklists (`insurer.py`), and job state transitions (`jobs.py`).
*   `src/carethread/errors.py`: Structured `GuidedErrorEnvelope` (`v1.0.0`), `ErrorCategory` enum, and `GuidedErrorException` returning actionable recovery instructions to the coordinator rather than raw stack traces.
*   `src/carethread/observability/`: Zero-PHI allowlist-first structured JSON logging (`logger.py`), OpenTelemetry W3C `traceparent` propagation (`tracing.py`), and active Sensitive Data Protection (`content.deidentify`) scrubbing that **fails closed** when SDP is unavailable (`redaction.py`).
*   `src/carethread/security/`: Deterministic tenant isolation, care-relationship verification, and HMAC-SHA256 `ScopeCapabilityToken` signing (`authz.py`), patient consent and expiring purpose-bound grant verification (`consent.py`), and Secret Manager integration with zero hardcoded keys (`secrets.py`).
*   `src/carethread/stores/`: Private CMEK GCS artifact store (`gcs.py`), authoritative versioned FHIR R4 writer with `Provenance`, conditional `If-None-Exist` / `If-Match` concurrency, and lost-response reconciliation (`fhir.py`), Firestore job state machine, 90s leases, and transactional outbox (`outbox.py`), and derived tenant-scoped Spanner Graph continuity index (`spanner_graph.py`).
*   `src/carethread/services/`: Verified continuity retrieval with FHIR `meta.versionId` freshness checks and authoritative FHIR R4 fallback (`continuity.py`), plus allowlisted signed-record insurer packet assembly and deterministic coverage checklist calculation (`insurer_release.py`).
*   `src/carethread/session/` & `src/carethread/hitl/`: Durable filtered ADK session state in Firestore (zero raw PHI/transcripts/audio paths) with bounded history compaction (`6` turns / `16,000` tokens) preserving active tool confirmations (`firestore_session.py`), and digest-bound HITL confirmation store (`confirmations.py`).
*   `src/carethread/prompts/`, `routing/`, `plugins/`: Clinical Documentation Constitution & untrusted-data boundary delimiters (`constitution_v1.py`), Strategic Model Router (`model_router.py`), and ADK `BasePlugin`-compatible policy gate emitting paired `ACTION_INTENT` / `ACTION_OUTCOME` logs (`policy_plugin.py`).
*   `src/carethread/tools/` & `agents/`: Strict `<verb>_<domain_noun>` tool registry & docstring validator (`registry.py`), the 8 versioned read/validate/HITL ADK tools (`clinical_tools.py` — zero LLM-callable FHIR write or packet dispatch tools), and the deterministic `CareThreadCoordinator` + bounded sequential pipeline (`coordinator.py`).
*   `evals/`: 100% fabricated synthetic fixtures (`evals/fixtures/synthetic_cases.py`) and the 10-case hard-gated Golden Evaluation Suite (`evals/test_golden_suite.py`).
*   `infra/terraform/`: Modular Google-provider (`hashicorp/google ~> 6.0` + `google-beta ~> 6.0`) Terraform IaC across 7 modules (`iam`, `storage`, `healthcare_fhir`, `spanner_graph` with ISO GQL `schema.sql`, `firestore`, `tasks_and_run`, and `security_obs`).
*   `cloudbuild.yaml`: Automated CI pipeline running AST hygiene checks, secret scanning, unit & IaC contract tests, Golden Evaluation hard gates, and `terraform fmt -check` + `terraform validate`.
*   `plan/` & `docs/`: Complete L200 architecture, low-level design, 19-criterion AgentOps coverage matrix, 5-minute reviewer walkthrough, MVP spec sheet, implementation plan, and `agents-cli` / `adk` runbook.

---

## 🧠 Core ADK Concepts & AgentOps Readiness (19-Criterion / 95-Point Rubric)

### 1. Tool & Interface Design (20 pts)
**Location:** `src/carethread/tools/registry.py`, `src/carethread/tools/clinical_tools.py`, `src/carethread/schemas/v1/`, `src/carethread/errors.py`
*   **Descriptive Naming & Comprehensive Docstrings:** `ToolRegistry` enforces `^[a-z]+_[a-z_]+$` naming (forbidding generic verbs like `update_record`, `handle`, `process`) and validates non-empty Google-style docstrings (`Args:`, `Returns:`, `Permissible Callers:`, `Side Effects:`) across all 8 ADK tools.
*   **Explicit Versioned JSON Schemas:** All payloads enforce `schema_version = "1.0.0"`, `extra="forbid"`, explicit `null` + `missing_reason` for absent clinical values, verified `evidence_ids`, and the 6-value `MedicationStatus` enum (`discussed`, `planned`, `prescribed`, `reported`, `stopped`, `unclear`).
*   **Guided Error Handling:** All tool failures return `GuidedErrorEnvelope` with structured `error_code`, `category`, `retryable`, `field_path`, and `guided_recovery` instructions.

### 2. Context & Memory Management (20 pts)
**Location:** `src/carethread/prompts/constitution_v1.py`, `src/carethread/session/firestore_session.py`, `src/carethread/stores/outbox.py`
*   **Robust System Instructions:** `CLINICAL_DOCUMENTATION_CONSTITUTION_V1` enforces role boundaries, untrusted-data isolation (`wrap_untrusted_clinical_data`), attribution/chronology/negation preservation, evidence grounding, and cross-visit separation.
*   **Bounded History Compaction:** `FilteredSessionStore.compact_events(max_turns=6, max_tokens=16000)` trims older turns while preserving active `requested_tool_confirmations` and unresolved tool calls.
*   **Persistent Filtered Session State:** Firestore-backed session store persists only workflow pointers, digests, and non-PHI summaries—rejecting raw PHI, transcripts, and audio paths while surviving container restarts.
*   **Async Memory Operations:** Signed-history Spanner Graph updates run asynchronously via transactional outbox + Cloud Tasks after FHIR R4 commit, never blocking clinician sign-off.

### 3. Orchestration, Strategic Model Routing & Guardrails (20 pts)
**Location:** `src/carethread/agents/coordinator.py`, `src/carethread/routing/model_router.py`, `src/carethread/plugins/policy_plugin.py`, `src/carethread/hitl/confirmations.py`
*   **Multi-Agent Pattern:** Deterministic `CareThreadCoordinator` orchestrating `VisitDraftSequentialPipeline` (`SoapExtractor -> EvidenceVerifier -> OptionalConflictReviewer`) with bounded retries (`max_regenerations = 2`).
*   **Strategic Model Routing:**
    *   *Audio Transcription:* `gemini-3.5-transcribe` (`VERBATIM`, diarization + word timestamps, 30-min cap) with deterministic fallback to Cloud Speech-to-Text V2 `chirp_3` (`BatchRecognize`).
    *   *SOAP Extraction & Continuity Brief:* `gemini-3.8-flash` (`thinking_level` in `LOW`/`MEDIUM`/`HIGH`, `response_schema` enforced).
    *   *Optional Conflict Review:* `gemini-4-argon` gated behind `enable_argon_second_opinion=True` and capped at `max_argon_escalations = 1`.
*   **Guardrails & Policy Plugin:** `CareThreadPolicyPlugin` verifies caller allowlists, tenant/scope token integrity, and untrusted-data delimiters before every tool and model call.
*   **Human-in-the-Loop Hooks:** `HitlConfirmationStore` binds clinician sign-off and insurer packet release to exact `(artifact_id, artifact_version, sha256_digest)`, failing closed on digest mismatch, expiry, consent revocation, or token replay.

### 4. Observability, Tracing & Zero-PHI Privacy (20 pts)
**Location:** `src/carethread/observability/`
*   **Structured JSON Logging:** Allowlist-first JSON records with correlation metadata (`trace_id`, `span_id`, `tenant_id`, `workflow`, `job_id`, `event_type`, `latency_ms`) and zero `print()` statements in `src/carethread/`.
*   **Intent vs. Outcome Capture:** Paired `ACTION_INTENT` and `ACTION_OUTCOME` logs linked by `action_id` and `trace_id` across tool calls, denials, and errors.
*   **Distributed Tracing:** OpenTelemetry spans with W3C `traceparent` propagation across API, ADK pipeline, Cloud Tasks headers, and background workers.
*   **Fail-Closed PII/PHI Redaction:** Strips forbidden keys unconditionally and actively scrubs residual free text via SDP (`content.deidentify`); drops free text and increments `sdp_unavailable_fail_closed` when SDP is unavailable.

### 5. Automated Golden Dataset Evaluation Suite & IaC/Secrets (15 pts)
**Location:** `evals/test_golden_suite.py`, `infra/terraform/`, `src/carethread/security/secrets.py`
*   **10 Hard-Gated Golden Cases (`GC-01`–`GC-10`):** Two-visit handoff, medication ambiguity & negation, failed authorization, stale graph fallback, changed consent, duplicate delivery & lost-response reconciliation, clinician rejection, patient identifiers in telemetry + SDP fail-closed, session survival across container restart, and allowlisted insurer packet + coverage checklist.
*   **Infrastructure as Code & Secure Secret Management:** Modular Terraform with 6 dedicated least-privilege service accounts (ADC), CMEK storage, versioned FHIR R4, Spanner Graph DDL, and Secret Manager references (zero hardcoded keys).

---

## 📋 Agent Input & Output Schemas (`v1.0.0`)

All CareThread agent contracts are strict Pydantic v2 models (`ConfigDict(extra="forbid")`, `schema_version = "1.0.0"`), orchestrated by `CareThreadCoordinator` (`src/carethread/agents/coordinator.py`).

### 1. Visit Draft Pipeline (`VisitDraftSequentialPipeline` / `run_visit_draft_pipeline`)
**Sub-agents:** `soap_extractor_agent` -> `evidence_verifier_agent` -> optional `conflict_reviewer_agent` (`gemini-4-argon`, max 1 escalation)

*   **Inputs:** `tenant_id`, `encounter_id`, `transcript_object_path` (private CMEK GCS path — never logged or stored in session), `session_id`, `user_id`, `soap_extractor`, optional `transcript_version_id`, `conflict_reviewer`, `trigger_conflict_review` (`False`), `max_regenerations` (`2`), `max_argon_escalations` (`1`), `known_fhir_refs`, `ttl_seconds` (`900`).
*   **Output (`VisitDraftPipelineResult`):**
```json
{
  "schema_version": "1.0.0",
  "job_id": "string",
  "session_id": "string",
  "status": "AWAITING_CLINICIAN_REVIEW | FAILED_TERMINAL",
  "draft": {
    "schema_version": "1.0.0",
    "draft_id": "DRF-2026-0131-0001",
    "draft_version": 1,
    "status": "DRAFT_AWAITING_CLINICIAN_REVIEW",
    "encounter_ref": { "encounter_id": "ENC-2026-0131-NEURO", "encounter_date": "2026-01-31", "specialty": "Neurology" },
    "source_refs": { "transcript_version_id": "TR-2026-0131-v1", "fhir_prior_refs": [] },
    "speaker_attribution": { "status": "UNVERIFIED_REQUIRES_CLINICIAN_CONFIRMATION", "candidates": [] },
    "soap": {
      "subjective": [{ "item_id": "SUBJ-01", "category": "chief_complaint", "statement": "...", "missing_reason": null, "polarity": "PRESENT | NEGATED | UNCERTAIN", "evidence_ids": ["SEG-004"] }],
      "objective": [{ "item_id": "OBJ-02", "category": "vital_signs", "statement": null, "missing_reason": "NOT_STATED_IN_TRANSCRIPT", "evidence_ids": [] }],
      "assessment": [{ "item_id": "ASSESS-01", "statement": "...", "certainty": "UNCERTAIN_WORKING_IMPRESSION", "evidence_ids": ["SEG-028"] }],
      "plan": {
        "medications": [{
          "item_id": "MED-01", "medication_name": "gabapentin", "dose": 300, "unit": "mg", "route": "oral", "frequency": "at bedtime",
          "missing_reason": null, "event_date": "2026-01-31",
          "medication_status": "discussed | planned | prescribed | reported | stopped | unclear",
          "status_rationale": "...", "evidence_ids": ["SEG-033"]
        }],
        "lifestyle_and_activity": [],
        "follow_up": []
      }
    },
    "review_flags": [{ "flag_id": "FLG-01", "severity": "REQUIRED_ACTION", "code": "VERIFY_SPEAKER_ROLES", "message": "..." }]
  },
  "draft_sha256": "64-char-hex-digest",
  "confirmation": "HitlConfirmation | null",
  "regeneration_count": 0,
  "argon_escalation_count": 0,
  "last_error": "GuidedErrorEnvelope | null",
  "trace_id": "string"
}
```

### 2. Continuity Retrieval Workflow (`continuity_retrieval_agent` / `run_continuity_brief_workflow`)
*   **Inputs:** `actor` (`ActorContext`), `patient_id`, `patient_tenant_id`, `consent_record` (`ConsentRecord`), `current_encounter` (`CurrentEncounterContext`), `as_of_date`, `session_id`, `max_encounters` (`10`, hard cap `25`), `requested_categories` (fail-closed subset of consent).
*   **Output (`ContinuityBriefPayload`):**
```json
{
  "schema_version": "1.0.0",
  "patient_id": "PT-SYN-0001",
  "continuity_source": "SPANNER_GRAPH_VERIFIED | FHIR_R4_AUTHORITATIVE",
  "staleness_banner": "string | null",
  "graph_watermark_ts": "ISO-8601 datetime | null",
  "prior_history_items": [{
    "item_id": "string",
    "encounter_id": "ENC-2026-0131-NEURO (must != current_encounter.encounter_id)",
    "encounter_date": "2026-01-31",
    "specialty": "Neurology",
    "practitioner_name": "Dr. Maya Lin",
    "category": "medication | problem | clinical_note",
    "summary": "string",
    "medication_status": "discussed | planned | prescribed | reported | stopped | unclear | null",
    "polarity": "PRESENT | NEGATED | UNCERTAIN | null",
    "certainty": "string | null",
    "evidence_ids": ["SEG-033"],
    "fhir_ref": { "resource_type": "MedicationStatement", "resource_id": "string", "expected_version_id": "1" }
  }],
  "current_encounter": { "encounter_id": "ENC-2026-0331-ORTHO", "encounter_date": "2026-03-31", "specialty": "Orthopedics" },
  "fhir_version_refs": [{ "resource_type": "Composition", "resource_id": "string", "expected_version_id": "1" }]
}
```

### 3. Insurer Release Workflow (`records_release_coordinator` / `run_insurer_packet_workflow`)
*   **Inputs:** `grant` (`ReleaseGrant`, `purpose == "prior_auth_documentation"` & unexpired), `consent_record` (`ConsentRecord` matching `consent_version_id`), `checklist_requirements`, `session_id`, `user_id` (`"records-reviewer"`), `packet_version` (`1`), `ttl_seconds` (`900`).
*   **Output (`InsurerPacketWorkflowResult`):**
```json
{
  "schema_version": "1.0.0",
  "job_id": "string",
  "session_id": "string",
  "status": "AWAITING_HUMAN_RELEASE",
  "packet": {
    "schema_version": "1.0.0",
    "packet_id": "PKT-0001",
    "packet_version": 1,
    "grant_id": "GRT-0001",
    "patient_id": "PT-SYN-0001",
    "consent_version_id": "CONSENT-v1",
    "purpose": "prior_auth_documentation",
    "status": "PREVIEW_AWAITING_HUMAN_RELEASE",
    "allowlisted_items": ["...signed FHIR R4 resources only..."],
    "coverage_checklist": { "numerator": 4, "denominator": 5, "missing_items": ["MRI lumbar spine report"] },
    "packet_sha256": "64-char-hex-digest"
  },
  "confirmation": "HitlConfirmation",
  "trace_id": "string"
}
```

### 4. Shared HITL Confirmation & Guided Error Contracts
*   **`HitlConfirmation` (`src/carethread/hitl/confirmations.py`):** `schema_version`, `confirmation_id`, `artifact_id`, `artifact_version`, `sha256_digest`, `kind`, `status` (`PENDING | APPROVED | REJECTED | EXPIRED | INVALIDATED_SUPERSEDED`), `created_at`, `expires_at`, `decided_by`, `decided_at`, `rejection_reason`, `superseded_by_version`, `superseded_by_digest`, `superseded_at`.
*   **`GuidedErrorEnvelope` (`src/carethread/errors.py`):** `schema_version` (`"1.0.0"`), `status` (`"ERROR"`), `error_code`, `category` (`AUTHZ_DENIED | CONSENT_REVOKED | STALE_GRAPH_DETECTED | VALIDATION_RECOVERABLE | HITL_DIGEST_MISMATCH | SDP_UNAVAILABLE_FAIL_CLOSED | GRANT_EXPIRED | OUT_OF_SCOPE_PURPOSE`), `retryable`, `max_retries_remaining`, `field_path`, `guided_recovery`, `trace_id`.

---

## ⚙️ Working Functionality (Implemented & Verified)

All functionality below is implemented in `src/carethread/` and continuously verified end-to-end across **129 passing tests** (`tests/` + `evals/test_golden_suite.py`):

| Capability | What Works Right Now | Verified By |
| :--- | :--- | :--- |
| **Jan 31 Neurology SOAP Drafting & Grounding** | Fetches diarized transcript segments from tenant-scoped private storage, wraps untrusted transcript text in anti-spoofing delimiters, runs schema-validated SOAP extraction + deterministic `evidence_id` verification with bounded retries (`max_regenerations=2`), and pauses at a digest-bound clinician HITL confirmation. | `GC-01`, `GC-02`, `tests/test_tools_and_coordinator.py` |
| **Medication Ambiguity, Negation & Injection Defense** | Rejects prompt-injection attempts to upgrade `gabapentin` from `discussed` to `prescribed`; preserves `stopped` (`ibuprofen`) and `NEGATED` red flags; `DeterministicFhirWriter` never creates a `MedicationRequest` for non-prescribed statuses. | `GC-02`, `tests/test_stores_fhir_outbox.py` |
| **Clinician HITL Sign-Off, Rejection & TOCTOU Protection** | Clinician approval requires exact `(artifact_version, sha256_digest)` match; rejecting a draft blocks FHIR writes (`REJECTED_BY_CLINICIAN`); editing a draft supersedes prior approvals (`INVALIDATED_SUPERSEDED`); expired or replayed tokens fail closed. | `GC-07`, `tests/test_session_and_hitl.py` |
| **Idempotent FHIR R4 Commit & Lost-Response Reconciliation** | Conditional `If-None-Exist` transactions commit `Composition`, `Condition`, `Observation`, `MedicationStatement`/`MedicationRequest`, and `Provenance` atomically; simulated lost responses transition to `RECONCILING_FHIR` and recover with zero duplicate resources. | `GC-06`, `tests/test_stores_fhir_outbox.py` |
| **Mar 31 Orthopedic Continuity & Authoritative Fallback** | Enforces deterministic tenant, care-relationship, consent, and `allowed_categories` checks (`ScopeCapabilityToken`), queries bounded Spanner Graph (`max_hops=2`, `max_encounters<=25`), verifies FHIR `meta.versionId`, falls back to `FHIR_R4_AUTHORITATIVE` on staleness/missing projection/timeout, and keeps prior visits structurally separate from the March encounter. | `GC-01`, `GC-03`, `GC-04`, `GC-05`, `tests/test_continuity_and_release.py` |
| **Purpose-Bound Insurer Packet & Coverage Checklist** | Validates expiring `prior_auth_documentation` grants and active patient consent, rejects out-of-scope purposes (`underwriting_risk_score`, `claims_adjudication`, `health_score`), excludes raw audio/drafts/session state, computes deterministic `CoverageChecklist(numerator, denominator, missing_items)`, and gates release behind HITL confirmation. | `GC-10`, `tests/test_security.py`, `tests/test_continuity_and_release.py` |
| **Zero-PHI Telemetry, Fail-Closed SDP & Restart Durability** | Strips forbidden keys, scrubs residual PHI via SDP (`content.deidentify`), drops free text and increments `sdp_unavailable_fail_closed` on SDP outage, propagates W3C `traceparent` spans, compacts filtered Firestore sessions (`6` turns / `16,000` tokens) without losing open HITL confirmations, and restores state cleanly across container restarts. | `GC-08`, `GC-09`, `tests/test_observability.py`, `tests/test_session_and_hitl.py` |

---

## 💻 How to Run & Evaluate Locally

```bash
# 1. Install dependencies into the virtual environment (using uv)
uv pip install --python .venv/bin/python pydantic pytest opentelemetry-api opentelemetry-sdk

# 2. Run all unit, integration, and IaC contract tests
.venv/bin/pytest tests/ -v

# 3. Run the 10-case hard-gated Golden Evaluation Suite & static hygiene gates
.venv/bin/pytest evals/test_golden_suite.py -v

# 4. Run the complete verification suite (129 tests)
.venv/bin/pytest tests/ evals/ -v
```

---

## 🚀 Agents CLI (`agents-cli`) & ADK Evidence

See [docs/build-with-agents-cli.md](docs/build-with-agents-cli.md) for the complete offline tool-registry inspection command, future live-ADK CLI templates (`agents-cli`, `adk eval`, `adk run`, `adk web`), and Secret Manager deployment prerequisites.

```bash
# Inspect all 8 registered CareThread ADK tools offline
PYTHONPATH=src .venv/bin/python -c "from evals.fixtures.synthetic_cases import build_eval_runtime; rt = build_eval_runtime(); print([(t.name, sorted(t.permissible_callers)) for t in rt.tool_registry.list_tools()])"
```

---

## ☁️ Infrastructure as Code (Terraform) & CI/CD

### Terraform Module Layout (`infra/terraform/`)
*   `main.tf`, `variables.tf`, `outputs.tf`: Root module pinning `hashicorp/google ~> 6.0` and `hashicorp/google-beta ~> 6.0` with parameterized `project_id`, `region`, and environment inputs (zero hardcoded project IDs or secrets).
*   `modules/iam/`: Provisions 6 dedicated least-privilege service accounts (`sa-api`, `sa-adk-runtime`, `sa-worker-transcribe`, `sa-worker-fhir`, `sa-worker-graph`, `sa-worker-release`). `sa-adk-runtime` has **zero** FHIR write (`roles/healthcare.fhirResourceEditor`) and **zero** raw-audio read permissions.
*   `modules/storage/`: Private CMEK-encrypted GCS buckets (`raw_audio`, `transcripts`, `sealed_packets`) with UBLA, public access prevention, versioning, and retention lifecycle rules.
*   `modules/healthcare_fhir/`: Cloud Healthcare API FHIR R4 dataset & store with `enable_update_create = false`, `disable_referential_integrity = false`, and resource versioning enabled.
*   `modules/spanner_graph/`: Spanner Enterprise instance, CMEK database, and `schema.sql` defining `CREATE PROPERTY GRAPH CareThreadContinuityGraph` over all 9 tenant-scoped node tables.
*   `modules/firestore/`: Regional Firestore Native database with TTL policies for filtered sessions, jobs, and outbox records.
*   `modules/tasks_and_run/`: Cloud Run v2 services and Cloud Tasks retry/deduplication queues.
*   `modules/security_obs/`: Identity Platform, Sensitive Data Protection de-identification template, Secret Manager secret shells, and Cloud Logging/Monitoring resources.

### Provisioning with Terraform
```bash
gcloud auth application-default login
cd infra/terraform
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -var="project_id=<YOUR_PROJECT_ID>" -var="region=us-central1"
```

### Automated CI Pipeline (`cloudbuild.yaml`)
`cloudbuild.yaml` runs on every PR/push:
1. Python AST hygiene (`zero print()` in `src/carethread/`) and secret-pattern scanning.
2. Unit, integration, and IaC contract tests (`pytest tests/ -v`).
3. 10-case Golden Evaluation hard gates (`pytest evals/test_golden_suite.py -v`).
4. Terraform formatting and validation (`terraform fmt -check -recursive && terraform validate` on `hashicorp/terraform:1.9`).

---

## 📚 Design & Architecture Documentation

*   **L200 Proposal (`plan/`):**
    *   [01-high-level-architecture.md](plan/01-high-level-architecture.md)
    *   [02-low-level-system-design.md](plan/02-low-level-system-design.md)
    *   [03-agentops-coverage-and-evaluations.md](plan/03-agentops-coverage-and-evaluations.md)
    *   [04-l200-reviewer-walkthrough.md](plan/04-l200-reviewer-walkthrough.md)
*   **Engineering Spec & Plan (`docs/superpowers/`):**
    *   [CareThread MVP Spec Sheet](docs/superpowers/specs/2026-11-24-carethread-mvp-spec.md)
    *   [CareThread MVP Implementation Plan](docs/superpowers/plans/2026-11-24-carethread-mvp-plan.md)
