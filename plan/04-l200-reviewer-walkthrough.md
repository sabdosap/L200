# CareThread — L200 Reviewer Walkthrough (5-Minute Storyboard)

> **Document Status:** Documentation-only L200 reviewer guide. Describes the planned five-minute walkthrough, what each screen would display in a future build, realistic build validation scope, primary references, and unresolved prerequisites.  
> **Consistent Invariants:** Cloud Healthcare API FHIR R4 is the sole signed clinical record; Spanner Graph is a derived continuity index; all writes, authorization, and consent checks are deterministic; clinician sign-off and insurer release require durable digest-bound HITL confirmation; routine telemetry is zero-PHI with fail-closed SDP scrubbing.

---

## 1. Five-Minute Reviewer Storyboard

| Time | Storyboard Beat | What the Screen Would Show (Future Build) | Architectural Guarantee Demonstrated |
| :--- | :--- | :--- | :--- |
| **0:00 – 0:45** | **1. Jan 31 Neurology Capture & Verbatim Transcript** | **Clinician Encounter View (`ENC-2026-0131-NEURO`):** Upload card for synthetic 8-minute two-person audio (`PT-SYN-0001`), job status chip (`TRANSCRIBED -> AWAITING_CLINICIAN_REVIEW`), and side-by-side verbatim transcript panel showing `SPEAKER_1` / `SPEAKER_2` labels with clickable word-level timestamps (`[00:42–00:58] SEG-004`). | Private CMEK GCS upload; prerecorded `gemini-3.5-transcribe` (`VERBATIM` mode); diarization explicitly marked *Unverified*. |
| **0:45 – 1:45** | **2. Evidence-Linked SOAP Draft, Speaker Verification & HITL Sign-Off** | **SOAP Review Drawer:** Structured S/O/A/P cards with clickable `[SEG-004]` evidence chips highlighting the exact transcript span. Two prominent review flags: (1) `VERIFY_SPEAKER_ROLES` toggle (`SPEAKER_1 = Dr. Maya Lin`, `SPEAKER_2 = Alex Rivera`), and (2) `MEDICATION_DISCUSSED_NOT_PRESCRIBED` badge on **gabapentin 300mg** (`status: discussed`) alongside **ibuprofen** (`status: stopped`) and **red flags** (`NEGATED`). Clicking **Sign & Commit** opens a confirmation modal displaying `draft_version: 1` and `sha256: 9f3c...a81b`. | Schema-enforced `gemini-3.8-flash` extraction; deterministic evidence verification; durable digest-bound HITL hook; FHIR R4 commit (`Composition` + `Provenance`, **zero** `MedicationRequest` for gabapentin). |
| **1:45 – 2:45** | **3. Mar 31 Orthopedic Continuity Handoff & Fallback Proof** | **Orthopedic Continuity Workspace (`ENC-2026-0331-ORTHO`):** Left rail clearly labeled **"Prior Signed History — 2026-01-31 (Neurology)"** visually separated from the right-hand **"Current Assessment — 2026-03-31 (Orthopedics)"** editor. Timeline displays dated complaints, functional limits, SLR finding, diet/walking advice, and gabapentin clearly tagged **`Discussed (Jan 31) — Not Prescribed / Current Use Unconfirmed`**. Clicking any fact opens its signed FHIR R4 `Provenance` + transcript span. A secondary toggle/test shows the subtle badge *"Live record view — continuity index refreshing"* when Spanner Graph staleness is simulated. | Tenant + consent check before read; bounded patient-first Spanner Graph traversal; FHIR R4 `meta.versionId` verification; transparent authoritative FHIR R4 fallback; strict cross-visit separation. |
| **2:45 – 3:45** | **4. Synthetic Insurer Release & Coverage Checklist** | **Records Release Reviewer Console:** Displays an active 72-hour grant (`purpose: prior_auth_documentation`), verified patient consent version, and a read-only preview of the **allowlisted signed-record packet** (signed SOAP `Composition` + `Provenance` only; raw audio/drafts/session state explicitly excluded). Side card shows the deterministic **Documentation Coverage Checklist** (e.g., `4 / 5 items documented`; Missing: `MRI lumbar spine report`). Reviewer clicks **Approve Release** against `(packet_version, sha256_digest)`. A second tab shows an automated `403` rejection when an out-of-scope `underwriting_risk_score` request is attempted. | Purpose-bound expiring grant; explicit patient authorization; HITL release gate; deterministic coverage checklist only; strict exclusion of health scoring, claims, eligibility, premiums, and underwriting. |
| **3:45 – 4:30** | **5. AgentOps Observability & Privacy Proof** | **Cloud Trace + Cloud Logging Split View:** Left pane shows a single OpenTelemetry trace spanning `API Gateway -> ADK Coordinator -> Tools -> Cloud Tasks -> FHIR Writer -> Graph Projection Worker`. Right pane shows paired `ACTION_INTENT` and `ACTION_OUTCOME` structured JSON logs with allowlisted metadata and SDP-redacted error fields (`[PERSON_NAME]` replaced) — zero raw prompts, transcripts, or audio paths. | Distributed tracing, structured JSON logging, Intent vs. Outcome capture, allowlist + fail-closed SDP PHI redaction. |
| **4:30 – 5:00** | **6. Automated Evals, IaC & Next-Step Gates** | **CI/CD & Toolchain Summary:** Displays the `adk eval` golden-case scorecard (all 10 hard-gated scenarios), `terraform plan` least-privilege service-account graph, and Secret Manager references (zero hardcoded keys). | Full traceability to the 19-criterion (95-point) AgentOps Review Matrix. |

---

## 2. What Can Realistically Be Validated During a Future MVP Build

| Validation Domain | Realistically Validated in Synthetic MVP Build | Deferred Beyond Onboarding MVP |
| :--- | :--- | :--- |
| **End-to-End Continuity Mechanics** | Complete Jan 31 -> Mar 31 synthetic flow using fabricated audio fixtures, `gemini-3.5-transcribe` (`VERBATIM`), `chirp_3` fallback, `gemini-3.8-flash` schema-constrained SOAP/brief generation, and FHIR R4 `Provenance` linking. | Real-world noisy multi-speaker (>2) exam rooms, clinical accents across 85+ locales, and production EHR SMART-on-FHIR write-back. |
| **Graph vs. Firestore Benchmark** | Controlled synthetic benchmark (e.g., 10k synthetic patients) measuring Spanner Graph vs. indexed Firestore p50/p95 traversal latency, async freshness lag, and cost-per-10k-queries. | Multi-region active-active Spanner topology and millions-of-patients production capacity planning. |
| **Security, Consent & HITL Hard Gates** | Automated CI proof of tenant isolation, consent revocation invalidation, digest-bound HITL sign-off/release, idempotent Cloud Tasks + FHIR lost-response reconciliation, and fail-closed SDP telemetry redaction. | External penetration testing, formal SOC 2 / HITRUST certification, and legal BAA sign-off. |
| **Model Routing Economics** | Offline `adk eval` A/B comparison of `gemini-3.8-flash` vs. `gemini-4-argon` on synthetic negation/attribution conflict fixtures to decide whether `gemini-4-argon` earns its routing gate. | Prospective clinical safety trials with licensed physicians evaluating real patient charts. |

---

## 3. Primary References

### Official Google Cloud & ADK Documentation
1. **Gemini Audio & Transcription (`gemini-3.5-transcribe` / `VERBATIM` mode & limits):** Google Cloud Vertex AI / Gemini Enterprise Agent Platform documentation (GA prerecorded unary 1 hr limit; 30-min cap with diarization/word timestamps; `-live` 10-min cap without native diarization).
2. **Cloud Speech-to-Text V2 (`chirp_3` fallback):** Google Cloud Speech-to-Text V2 documentation (`BatchRecognize`-only diarization with `SpeakerDiarizationConfig`, regional availability, locale GA/Preview matrix, and word-timestamp/confidence caveats).
3. **Gemini Reasoning Models (`gemini-3.8-flash` & `gemini-4-argon`):** Google Cloud Vertex AI model documentation (`GenerateContent` / Interactions API, 1M context window, 65,536 default output / long decode on Argon, structured JSON `response_schema`, `LOW`/`MEDIUM`/`HIGH` thinking levels).
4. **Google ADK for Python & Google Agents CLI (`agents-cli`, `adk`):** Official ADK documentation (`BasePlugin`, `EventsCompactionConfig`, `BaseSessionService`, `tool_context.request_confirmation()`, `adk eval`).
5. **Cloud Healthcare API (FHIR R4):** Google Cloud Healthcare API FHIR R4 documentation (`enableResourceVersioning`, `meta.versionId`, `vread`, `_history`, `If-Match`, `_revinclude=Provenance:target`, 10 MB resource / 50 MB bundle limits, regional datasets).
6. **Cloud Spanner Graph & Firestore:** Google Cloud Spanner Graph ISO GQL / `CREATE PROPERTY GRAPH` documentation (Enterprise/Enterprise Plus editions, PU pricing) and Firestore Native limits (1 MiB doc, 1 write/sec/doc, consumption pricing).
7. **Sensitive Data Protection (Cloud DLP):** Google Cloud SDP `content.deidentify` documentation (targeted PHI `infoTypes`, regional endpoints, `dlp.googleapis.com/quota_tokens`).
8. **Cloud Run, Cloud Tasks, Identity Platform, Secret Manager, Cloud Build, Artifact Registry, Cloud Logging/Trace/Monitoring, OpenTelemetry & Terraform Google Provider:** Standard Google Cloud platform documentation.

### Read-Only Reference Repositories
- **PocketSage:** `https://github.com/The-Liberal-Labs/PocketSage` — referenced for speaker-labelled appointment transcription, dual-view Gemini summaries, and user-to-appointment graph concepts.
- **ADK Healthcare Agent:** `https://github.com/phanisagiraju-byte/adk-healthcare-agent` — referenced for ADK critic loop, durable session state, clinician sign-off, and golden evaluation patterns.
- *Inspection Note:* Direct GitHub API file-tree access was blocked by environment policy during this documentation-only session; no repository-specific infrastructure, project IDs, or scripts were reused, and exact file-level citations should be verified in a network-enabled review prior to implementation.

---

## 4. Unresolved Project Prerequisites

### A. Pre-Build Onboarding Prerequisites (Must Resolve Before Starting MVP Code)
1. **Program Owner Rubric Confirmation:** Confirm whether L200 documentation sign-off precedes implementation and confirm the exact code/CLI/screenshot evidence required for final 95-point scoring.
2. **GCP Project, Region & Quota Verification:** Confirm target sandbox project ID, co-located primary region (e.g., `us-central1`), IAM permissions to provision Terraform modules, and active quotas for `gemini-3.5-transcribe`, `gemini-3.8-flash`, `chirp_3`, `gemini-4-argon` (if eval-tested), and SDP (`dlp.googleapis.com/quota_tokens`).
3. **Spanner Enterprise Budget Approval:** Confirm whether the onboarding sandbox budget covers a minimal Spanner Enterprise instance (100 PU) for the Graph benchmark, or if the initial pilot should run on the documented Firestore projection fallback first.
4. **Synthetic Fixture Pack Sign-Off:** Approve the fabricated 2-visit Neurology/Orthopedics script, synthetic audio recordings (<15 min, 2 speakers), and synthetic insurer checklist template (confirming 0% real PHI).

### B. Real-Data Production Deployment Questions (Out of Scope for Onboarding, Required Before Any Real PHI)
1. **Regulatory & Legal:** What HIPAA BAA, state privacy law, and GDPR/data-residency agreements cover every model endpoint (`gemini-3.5-transcribe`, `gemini-3.8-flash`, `gemini-4-argon`) and region in the chosen stack?
2. **Clinical Governance & Safety:** What clinical safety review board approves the SOAP Constitution, review-flag thresholds, and clinician sign-off UX (including liability for uncorrected diarization or omission errors)?
3. **Patient Consent & Recording Laws:** How are two-party/all-party audio recording consent laws enforced before audio capture begins, and how does patient consent revocation propagate to downstream EHRs and previously released packets?
4. **EHR & Identity Integration:** Which hospital identity provider (SAML/OIDC federation) and EHR SMART-on-FHIR endpoints will replace synthetic Identity Platform accounts and standalone FHIR R4 stores?
5. **Retention & Legal Hold:** What exact tenant/jurisdiction retention windows govern raw audio deletion vs. signed FHIR `Composition` + `Provenance` legal medical record archival?
6. **Insurer Data-Sharing Governance:** What formal Data Use Agreements (DUAs) and payer-specific minimum-necessary allowlists govern real prior-authorization packets?
