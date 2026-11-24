"""All 8 versioned CareThread ADK tools (read, validate, and HITL pause only — zero FHIR writes or dispatch)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
import json
import re
from typing import Any, Literal
import uuid

from pydantic import Field, ValidationError

from carethread.errors import ErrorCategory, GuidedErrorEnvelope
from carethread.hitl.confirmations import HitlConfirmation, HitlConfirmationStore
from carethread.schemas import StrictModel
from carethread.schemas.v1.continuity import FhirVersionRef
from carethread.schemas.v1.insurer import InsurerPacketPayload
from carethread.schemas.v1.soap import (
    MedicationItem,
    MedicationStatus,
    SoapDraftPayload,
    SoapItem,
    TranscriptSegment,
    compute_canonical_sha256,
)
from carethread.security.authz import ScopeCapabilityToken
from carethread.security.consent import ConsentRecord, ReleaseGrant
from carethread.services.continuity import ContinuityService, VerifiedContinuityResult
from carethread.services.insurer_release import ChecklistRequirement, InsurerReleaseService
from carethread.stores.fhir import FhirR4Store, FhirResourceNotFoundError
from carethread.stores.gcs import PrivateArtifactStore
from carethread.stores.spanner_graph import (
    MAX_HOPS_LIMIT,
    GraphContinuitySnapshot,
    SpannerGraphStore,
)
from carethread.tools.registry import ToolRegistry

EXPECTED_ADK_TOOL_NAMES: tuple[str, ...] = (
    "fetch_verbatim_transcript_segments",
    "validate_soap_draft_grounding",
    "request_clinician_signoff_confirmation",
    "query_patient_continuity_graph",
    "verify_fhir_resource_versions",
    "fetch_authoritative_fhir_history",
    "assemble_allowlisted_insurer_packet",
    "request_release_hitl_confirmation",
)

_PROMPT_INJECTION_MED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+override\b", re.IGNORECASE),
    re.compile(r"\b(?:mark|set|upgrade|classify)\b.{0,40}\bprescribed\b", re.IGNORECASE),
    re.compile(r"\bactive\s+order\s+status\b", re.IGNORECASE),
)

_NON_PRESCRIPTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bnot\s+prescribing\b", re.IGNORECASE),
    re.compile(r"\b(?:did\s+not|didn't|won't|will\s+not)\s+prescribe\b", re.IGNORECASE),
    re.compile(r"\bno\s+prescription\b", re.IGNORECASE),
    re.compile(r"\bholding\s+off\b", re.IGNORECASE),
    re.compile(r"\bdefer(?:ring|red)?\b", re.IGNORECASE),
    re.compile(r"\bwithout\s+prescribing\b", re.IGNORECASE),
    re.compile(r"\bnot\s+prescribed\b", re.IGNORECASE),
    re.compile(r"\bdiscussed\s+only\b", re.IGNORECASE),
)

_AFFIRMATIVE_PRESCRIPTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:i\s+am|i'm|we\s+are|we'll|will)\s+(?:prescribing|starting|ordering)\b", re.IGNORECASE),
    re.compile(r"\bprescrib(?:e|ed|es|ing)\b", re.IGNORECASE),
    re.compile(r"\bordering\b", re.IGNORECASE),
    re.compile(r"\bstarted\b.{0,40}\bprescription\b", re.IGNORECASE),
    re.compile(r"\bstarting\s+you\s+on\b", re.IGNORECASE),
    re.compile(r"\bsending\s+(?:a\s+)?(?:prescription|rx)\b", re.IGNORECASE),
    re.compile(r"\brx\b", re.IGNORECASE),
)


class TranscriptSegmentsResult(StrictModel):
    """Structured output returned by fetch_verbatim_transcript_segments."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    transcript_ref: str = Field(min_length=1)
    transcript_version_id: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    segments: list[TranscriptSegment] = Field(default_factory=list)


class GroundingValidationResult(StrictModel):
    """Structured output returned when validate_soap_draft_grounding succeeds."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    status: Literal["VALID"] = "VALID"
    draft_id: str = Field(min_length=1)
    draft_version: int = Field(ge=1)
    draft_sha256: str = Field(min_length=64, max_length=64)
    verified_evidence_ids: list[str] = Field(default_factory=list)


class FhirVersionVerificationResult(StrictModel):
    """Structured output returned by verify_fhir_resource_versions."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    all_fresh: bool
    verified_refs: list[FhirVersionRef] = Field(default_factory=list)
    stale_refs: list[FhirVersionRef] = Field(default_factory=list)
    missing_refs: list[FhirVersionRef] = Field(default_factory=list)


def _check_medication_status_discipline(
    med: MedicationItem,
    segment_map: dict[str, TranscriptSegment],
) -> str | None:
    """Fail-closed check: PRESCRIBED requires medication name + affirmative prescription phrase without injection/negation."""
    if med.medication_status != MedicationStatus.PRESCRIBED:
        return None

    cited_texts = [
        segment_map[eid].text
        for eid in med.evidence_ids
        if eid in segment_map
    ]
    if not cited_texts:
        return (
            f"Medication '{med.medication_name}' is marked 'prescribed' without "
            "verifiable verbatim transcript segment evidence."
        )

    combined_evidence = " ".join(cited_texts)
    rationale = med.status_rationale

    for pattern in _PROMPT_INJECTION_MED_PATTERNS:
        if pattern.search(combined_evidence):
            return (
                f"Medication '{med.medication_name}' cannot be upgraded to 'prescribed' "
                "based on transcript text containing prompt-injection or override phrasing."
            )

    for pattern in _NON_PRESCRIPTION_PATTERNS:
        if pattern.search(combined_evidence) or pattern.search(rationale):
            return (
                f"Medication '{med.medication_name}' is marked 'prescribed' despite "
                "explicit non-prescription or discussion-only evidence."
            )

    med_name_pattern = re.compile(rf"\b{re.escape(med.medication_name.strip())}\b", re.IGNORECASE)
    if not med_name_pattern.search(combined_evidence):
        return (
            f"Medication '{med.medication_name}' is marked 'prescribed' but its name "
            "does not appear in the cited verbatim transcript segment(s)."
        )

    has_affirmative = any(p.search(combined_evidence) for p in _AFFIRMATIVE_PRESCRIPTION_PATTERNS)
    if not has_affirmative:
        return (
            f"Medication '{med.medication_name}' is marked 'prescribed' without an "
            "affirmative clinician prescription statement in the cited verbatim transcript segment(s)."
        )

    return None


def validate_soap_draft_grounding(
    soap_draft: SoapDraftPayload | dict[str, Any],
    transcript_segments: Sequence[TranscriptSegment | dict[str, Any]],
    *,
    known_fhir_refs: Iterable[str] | None = None,
    max_retries_remaining: int = 0,
    trace_id: str | None = None,
) -> GroundingValidationResult | GuidedErrorEnvelope:
    """Deterministically validate SOAP draft schema, evidence_id grounding, and medication status discipline.

    Args:
        soap_draft: Candidate SoapDraftPayload or JSON-compatible mapping to verify.
        transcript_segments: Verbatim transcript segments from the current encounter.
        known_fhir_refs: Optional verified signed FHIR R4 reference strings allowed as citations.
        max_retries_remaining: Remaining bounded regeneration attempts for recovery guidance.
        trace_id: Optional W3C trace identifier for structured error envelopes.

    Returns:
        GroundingValidationResult when valid, or a retryable GuidedErrorEnvelope on validation failure.

    Permissible Callers:
        evidence_verifier_agent, adk_coordinator

    Side Effects:
        None (pure deterministic read-only validation).
    """
    resolved_trace = trace_id or uuid.uuid4().hex

    try:
        draft = (
            soap_draft
            if isinstance(soap_draft, SoapDraftPayload)
            else SoapDraftPayload.model_validate(soap_draft)
        )
    except ValidationError as exc:
        first_err = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first_err.get("loc", ())) or "soap_draft"
        return GuidedErrorEnvelope(
            error_code="SOAP_SCHEMA_VALIDATION_FAILED",
            category=ErrorCategory.VALIDATION_RECOVERABLE,
            retryable=True,
            max_retries_remaining=max(0, max_retries_remaining),
            field_path=loc,
            guided_recovery=(
                f"Fix SOAP draft schema violation at '{loc}': {first_err.get('msg', 'invalid schema')}."
            ),
            trace_id=resolved_trace,
        )

    parsed_segments: list[TranscriptSegment] = [
        seg if isinstance(seg, TranscriptSegment) else TranscriptSegment.model_validate(seg)
        for seg in transcript_segments
    ]
    segment_map = {seg.evidence_id: seg for seg in parsed_segments}
    valid_evidence_ids = set(segment_map.keys()) | set(known_fhir_refs or ())

    cited_evidence_ids: set[str] = set()

    def _verify_soap_items(items: Sequence[SoapItem], section_path: str) -> GuidedErrorEnvelope | None:
        for idx, item in enumerate(items):
            for eid in item.evidence_ids:
                if eid not in valid_evidence_ids:
                    return GuidedErrorEnvelope(
                        error_code="EVIDENCE_SPAN_NOT_FOUND",
                        category=ErrorCategory.VALIDATION_RECOVERABLE,
                        retryable=True,
                        max_retries_remaining=max(0, max_retries_remaining),
                        field_path=f"{section_path}[{idx}].evidence_ids",
                        guided_recovery=(
                            f"Cited evidence_id '{eid}' does not exist in verbatim transcript segments; "
                            "cite only verified evidence_id values from the current encounter."
                        ),
                        trace_id=resolved_trace,
                    )
                cited_evidence_ids.add(eid)
        return None

    for items, path in (
        (draft.soap.subjective, "soap.subjective"),
        (draft.soap.objective, "soap.objective"),
        (draft.soap.assessment, "soap.assessment"),
        (draft.soap.plan.lifestyle_and_activity, "soap.plan.lifestyle_and_activity"),
        (draft.soap.plan.follow_up, "soap.plan.follow_up"),
    ):
        err = _verify_soap_items(items, path)
        if err is not None:
            return err

    for idx, med in enumerate(draft.soap.plan.medications):
        for eid in med.evidence_ids:
            if eid not in valid_evidence_ids:
                return GuidedErrorEnvelope(
                    error_code="EVIDENCE_SPAN_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=True,
                    max_retries_remaining=max(0, max_retries_remaining),
                    field_path=f"soap.plan.medications[{idx}].evidence_ids",
                    guided_recovery=(
                        f"Cited evidence_id '{eid}' for medication '{med.medication_name}' does not exist "
                        "in verbatim transcript segments."
                    ),
                    trace_id=resolved_trace,
                )
            cited_evidence_ids.add(eid)

        med_violation = _check_medication_status_discipline(med, segment_map)
        if med_violation is not None:
            return GuidedErrorEnvelope(
                error_code="MEDICATION_STATUS_UPGRADE_VIOLATION",
                category=ErrorCategory.VALIDATION_RECOVERABLE,
                retryable=True,
                max_retries_remaining=max(0, max_retries_remaining),
                field_path=f"soap.plan.medications[{idx}].medication_status",
                guided_recovery=(
                    f"{med_violation} Classify medication_status strictly from verbatim evidence "
                    "(discussed, planned, prescribed, reported, stopped, or unclear)."
                ),
                trace_id=resolved_trace,
            )

    return GroundingValidationResult(
        draft_id=draft.draft_id,
        draft_version=draft.draft_version,
        draft_sha256=compute_canonical_sha256(draft),
        verified_evidence_ids=sorted(cited_evidence_ids),
    )


def build_clinical_tool_registry(
    *,
    artifact_store: PrivateArtifactStore,
    fhir_store: FhirR4Store,
    graph_store: SpannerGraphStore,
    continuity_service: ContinuityService,
    insurer_release_service: InsurerReleaseService,
    hitl_store: HitlConfirmationStore,
) -> ToolRegistry:
    """Register all 8 versioned CareThread ADK tools with strict naming and docstring validation."""
    registry = ToolRegistry()

    def fetch_verbatim_transcript_segments(
        tenant_id: str,
        object_path: str,
        version_id: str | None = None,
    ) -> TranscriptSegmentsResult:
        """Fetch immutable verbatim speaker-diarized transcript segments from private tenant storage.

        Args:
            tenant_id: Authenticated tenant boundary identifier.
            object_path: Tenant-scoped GCS object path for the verbatim transcript JSON.
            version_id: Optional immutable artifact version identifier.

        Returns:
            TranscriptSegmentsResult containing validated TranscriptSegment entries and SHA-256 metadata.

        Permissible Callers:
            soap_extractor_agent, evidence_verifier_agent

        Side Effects:
            None (read-only artifact retrieval; never accesses raw audio).
        """
        raw_bytes, meta = artifact_store.get_artifact(
            tenant_id=tenant_id,
            object_path=object_path,
            version_id=version_id,
        )
        parsed = json.loads(raw_bytes.decode("utf-8"))
        raw_segments = parsed["segments"] if isinstance(parsed, dict) and "segments" in parsed else parsed
        segments = [TranscriptSegment.model_validate(item) for item in raw_segments]
        return TranscriptSegmentsResult(
            transcript_ref=meta.uri,
            transcript_version_id=meta.version_id,
            sha256=meta.sha256,
            segments=segments,
        )

    def request_clinician_signoff_confirmation(
        draft_id: str,
        draft_version: int,
        draft_sha256: str,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> HitlConfirmation:
        """Create a durable digest-bound HITL clinician sign-off confirmation request.

        Args:
            draft_id: Identifier of the validated SOAP draft awaiting clinician review.
            draft_version: Monotonic version number of the SOAP draft.
            draft_sha256: Canonical SHA-256 hex digest bound to the exact SOAP draft payload.
            ttl_seconds: Expiration window in seconds for the pending confirmation.
            now: Optional UTC timestamp override for deterministic execution.

        Returns:
            HitlConfirmation persisted in PENDING status bound to (draft_id, draft_version, draft_sha256).

        Permissible Callers:
            adk_coordinator

        Side Effects:
            Persists a PENDING HitlConfirmation record; does NOT write to FHIR R4.
        """
        return hitl_store.create_confirmation(
            artifact_id=draft_id,
            artifact_version=draft_version,
            sha256_digest=draft_sha256,
            kind="clinician_soap_signoff",
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def query_patient_continuity_graph(
        scope_token: ScopeCapabilityToken,
        as_of_date: date | None = None,
        max_encounters: int = 10,
        max_hops: int = MAX_HOPS_LIMIT,
    ) -> GraphContinuitySnapshot:
        """Execute a bounded patient-first traversal over the derived Spanner Graph continuity index.

        Args:
            scope_token: Verified short-lived ScopeCapabilityToken pinning tenant_id and patient_id.
            as_of_date: Optional upper encounter date boundary for point-in-time continuity retrieval.
            max_encounters: Maximum prior encounters to retrieve (1..25).
            max_hops: Maximum graph traversal hops (1..2).

        Returns:
            GraphContinuitySnapshot containing bounded encounters, events, and FHIR version references.

        Permissible Callers:
            continuity_retrieval_agent

        Side Effects:
            None (read-only query against derived Spanner Graph index).
        """
        return graph_store.query_patient_continuity(
            tenant_id=scope_token.tenant_id,
            patient_id=scope_token.patient_id,
            max_encounters=max_encounters,
            max_hops=max_hops,
            as_of_date=as_of_date,
        )

    def verify_fhir_resource_versions(
        scope_token: ScopeCapabilityToken,
        fhir_version_refs: Sequence[FhirVersionRef],
    ) -> FhirVersionVerificationResult:
        """Verify derived graph FHIR resource versionIds against authoritative Cloud Healthcare FHIR R4.

        Args:
            scope_token: Verified ScopeCapabilityToken pinning the tenant boundary.
            fhir_version_refs: Candidate FhirVersionRef entries returned from Spanner Graph.

        Returns:
            FhirVersionVerificationResult reporting freshness, stale references, and missing references.

        Permissible Callers:
            continuity_retrieval_agent, evidence_verifier_agent

        Side Effects:
            None (read-only FHIR R4 meta.versionId lookup).
        """
        verified: list[FhirVersionRef] = []
        stale: list[FhirVersionRef] = []
        missing: list[FhirVersionRef] = []

        for ref in fhir_version_refs:
            try:
                latest = fhir_store.read(scope_token.tenant_id, ref.resource_type, ref.resource_id)
            except FhirResourceNotFoundError:
                missing.append(ref)
                continue
            latest_version = str((latest.get("meta") or {}).get("versionId", ""))
            if latest_version == ref.version_id:
                verified.append(ref)
            else:
                stale.append(ref)

        return FhirVersionVerificationResult(
            all_fresh=not stale and not missing,
            verified_refs=verified,
            stale_refs=stale,
            missing_refs=missing,
        )

    def fetch_authoritative_fhir_history(
        scope_token: ScopeCapabilityToken,
        as_of_date: date,
        max_encounters: int = 10,
        current_encounter_id: str | None = None,
        fallback_reason: str = "AUTHORITATIVE_FHIR_DIRECT_READ",
        staleness_banner: str = "Served directly from authoritative FHIR R4.",
        now: datetime | None = None,
    ) -> VerifiedContinuityResult:
        """Read patient continuity history directly from authoritative FHIR R4 as the fallback path.

        Args:
            scope_token: Verified ScopeCapabilityToken pinning tenant, patient, and allowed categories.
            as_of_date: Upper encounter date boundary for longitudinal history retrieval.
            max_encounters: Maximum prior encounters to return (1..25).
            current_encounter_id: Optional active encounter ID excluded to preserve cross-visit separation.
            fallback_reason: Structured reason code explaining why authoritative fallback was invoked.
            staleness_banner: Clinician-facing provenance banner noting authoritative FHIR R4 retrieval.
            now: Optional UTC timestamp for token expiry verification.

        Returns:
            VerifiedContinuityResult sourced directly from authoritative FHIR R4.

        Permissible Callers:
            continuity_retrieval_agent

        Side Effects:
            None (read-only authoritative FHIR R4 retrieval).
        """
        return continuity_service.fetch_authoritative_history(
            scope_token=scope_token,
            as_of_date=as_of_date,
            max_encounters=max_encounters,
            current_encounter_id=current_encounter_id,
            fallback_reason=fallback_reason,
            staleness_banner=staleness_banner,
            now=now,
        )

    def assemble_allowlisted_insurer_packet(
        grant: ReleaseGrant,
        consent: ConsentRecord,
        checklist_requirements: Sequence[str | ChecklistRequirement],
        now: datetime,
        packet_id: str | None = None,
        packet_version: int = 1,
    ) -> InsurerPacketPayload:
        """Assemble an allowlisted signed-FHIR insurer prior-auth packet with deterministic checklist.

        Args:
            grant: Active, purpose-bound ReleaseGrant pinned to consent_version_id.
            consent: Active patient ConsentRecord matching the grant.
            checklist_requirements: Prior-authorization documentation requirements to score deterministically.
            now: Current UTC timestamp for grant expiry verification.
            packet_id: Optional deterministic packet identifier.
            packet_version: Monotonic packet version number.

        Returns:
            InsurerPacketPayload containing only allowlisted signed FHIR items, checklist, and packet_sha256.

        Permissible Callers:
            records_release_coordinator

        Side Effects:
            None (read-only allowlist assembly; never dispatches externally).
        """
        return insurer_release_service.assemble_allowlisted_packet(
            grant=grant,
            consent=consent,
            checklist_requirements=checklist_requirements,
            now=now,
            packet_id=packet_id,
            packet_version=packet_version,
        )

    def request_release_hitl_confirmation(
        packet_id: str,
        packet_version: int,
        packet_sha256: str,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> HitlConfirmation:
        """Create a durable digest-bound HITL confirmation request for human insurer packet release.

        Args:
            packet_id: Identifier of the assembled insurer release packet.
            packet_version: Monotonic packet version number.
            packet_sha256: Canonical SHA-256 hex digest bound to the exact insurer packet payload.
            ttl_seconds: Expiration window in seconds for the pending confirmation.
            now: Optional UTC timestamp override for deterministic execution.

        Returns:
            HitlConfirmation persisted in PENDING status bound to (packet_id, packet_version, packet_sha256).

        Permissible Callers:
            records_release_coordinator

        Side Effects:
            Persists a PENDING HitlConfirmation record; does NOT dispatch the insurer packet.
        """
        return hitl_store.create_confirmation(
            artifact_id=packet_id,
            artifact_version=packet_version,
            sha256_digest=packet_sha256,
            kind="insurer_packet_release",
            ttl_seconds=ttl_seconds,
            now=now,
        )

    registry.register(
        fetch_verbatim_transcript_segments,
        permissible_callers={"soap_extractor_agent", "evidence_verifier_agent"},
    )
    registry.register(
        validate_soap_draft_grounding,
        permissible_callers={"evidence_verifier_agent", "adk_coordinator"},
    )
    registry.register(
        request_clinician_signoff_confirmation,
        permissible_callers={"adk_coordinator"},
    )
    registry.register(
        query_patient_continuity_graph,
        permissible_callers={"continuity_retrieval_agent"},
    )
    registry.register(
        verify_fhir_resource_versions,
        permissible_callers={"continuity_retrieval_agent", "evidence_verifier_agent"},
    )
    registry.register(
        fetch_authoritative_fhir_history,
        permissible_callers={"continuity_retrieval_agent"},
    )
    registry.register(
        assemble_allowlisted_insurer_packet,
        permissible_callers={"records_release_coordinator"},
    )
    registry.register(
        request_release_hitl_confirmation,
        permissible_callers={"records_release_coordinator"},
    )

    return registry


__all__ = [
    "EXPECTED_ADK_TOOL_NAMES",
    "FhirVersionVerificationResult",
    "GroundingValidationResult",
    "TranscriptSegmentsResult",
    "build_clinical_tool_registry",
    "validate_soap_draft_grounding",
]
