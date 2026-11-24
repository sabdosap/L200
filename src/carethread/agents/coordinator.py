"""Deterministic CareThread ADK Coordinator and Sequential VisitDraft Pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timezone
from typing import Any, Literal
import uuid

from pydantic import Field

from carethread.errors import GuidedErrorEnvelope, GuidedErrorException
from carethread.hitl.confirmations import HitlConfirmation
from carethread.plugins.policy_plugin import CareThreadPolicyPlugin
from carethread.prompts.constitution_v1 import (
    CLINICAL_DOCUMENTATION_CONSTITUTION_V1,
    wrap_untrusted_clinical_data,
)
from carethread.routing.model_router import MAX_ARGON_ESCALATIONS, ModelRouteConfig, ModelRouter
from carethread.schemas import StrictModel
from carethread.schemas.v1.continuity import (
    ContinuityBriefPayload,
    ContinuitySource,
    CurrentEncounterContext,
)
from carethread.schemas.v1.insurer import InsurerPacketPayload
from carethread.schemas.v1.jobs import JobState
from carethread.schemas.v1.soap import SoapDraftPayload, TranscriptSegment
from carethread.security.authz import ActorContext, AuthzService
from carethread.security.consent import ConsentRecord, ReleaseGrant
from carethread.services.continuity import (
    ContinuityService,
    VerifiedContinuityResult,
    _collect_fhir_version_refs,
    _snapshot_to_prior_items,
)
from carethread.services.insurer_release import ChecklistRequirement
from carethread.session.firestore_session import FilteredSessionStore
from carethread.stores.spanner_graph import (
    MAX_HOPS_LIMIT,
    SpannerGraphTimeoutError,
    SpannerGraphUnavailableError,
)
from carethread.tools.clinical_tools import (
    FhirVersionVerificationResult,
    GroundingValidationResult,
    TranscriptSegmentsResult,
)
from carethread.tools.registry import ToolRegistry

DEFAULT_MAX_REGENERATIONS: int = 2

SoapExtractorFn = Callable[
    [str, list[TranscriptSegment], ModelRouteConfig, GuidedErrorEnvelope | None, int],
    SoapDraftPayload | dict[str, Any],
]

ConflictReviewerFn = Callable[
    [str, SoapDraftPayload, list[TranscriptSegment], ModelRouteConfig],
    SoapDraftPayload | dict[str, Any],
]


class VisitDraftPipelineResult(StrictModel):
    """Outcome of the bounded sequential VisitDraft pipeline."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    job_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    status: JobState
    draft: SoapDraftPayload | None = None
    draft_sha256: str | None = None
    confirmation: HitlConfirmation | None = None
    regeneration_count: int = Field(default=0, ge=0)
    argon_escalation_count: int = Field(default=0, ge=0)
    last_error: GuidedErrorEnvelope | None = None
    trace_id: str = Field(min_length=1)


class InsurerPacketWorkflowResult(StrictModel):
    """Outcome of the insurer prior-auth packet workflow paused at HITL release."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    job_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    status: Literal["AWAITING_HUMAN_RELEASE"] = "AWAITING_HUMAN_RELEASE"
    packet: InsurerPacketPayload
    confirmation: HitlConfirmation
    trace_id: str = Field(min_length=1)


def _render_segments_for_prompt(segments: Sequence[TranscriptSegment]) -> str:
    return "\n".join(
        f"[{seg.evidence_id}] ({seg.speaker_label} {seg.start_ms}-{seg.end_ms}ms): {seg.text}"
        for seg in segments
    )


def _build_extraction_prompt(
    *,
    encounter_id: str,
    segments: Sequence[TranscriptSegment],
    feedback: GuidedErrorEnvelope | None,
) -> str:
    wrapped_transcript = wrap_untrusted_clinical_data(
        "verbatim_transcript",
        _render_segments_for_prompt(segments),
    )
    parts = [
        CLINICAL_DOCUMENTATION_CONSTITUTION_V1,
        f"Encounter ID: {encounter_id}",
        "Extract a structured v1.0.0 SOAP draft grounded strictly in the verbatim transcript below.",
        wrapped_transcript,
    ]
    if feedback is not None:
        parts.append(
            f"Validation Feedback ({feedback.error_code} at {feedback.field_path}): "
            f"{feedback.guided_recovery}"
        )
    return "\n\n".join(parts)


def _has_conflict_review_trigger(draft: SoapDraftPayload) -> bool:
    for flag in draft.review_flags:
        if flag.severity in ("REQUIRED_ACTION", "CLINICAL_ATTENTION") and (
            "CONFLICT" in flag.code.upper() or "DISCREPANCY" in flag.code.upper()
        ):
            return True
    return False


class VisitDraftSequentialPipeline:
    """Bounded Sequential pipeline: SoapExtractor -> EvidenceVerifier -> OptionalConflictReviewer."""

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        policy_plugin: CareThreadPolicyPlugin,
        model_router: ModelRouter,
        session_store: FilteredSessionStore,
    ) -> None:
        self._tool_registry = tool_registry
        self._policy_plugin = policy_plugin
        self._model_router = model_router
        self._session_store = session_store

    def _ensure_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        initial_state: dict[str, Any],
        now: datetime,
    ) -> None:
        try:
            self._session_store.get_session(session_id)
            self._session_store.update_state(session_id, initial_state, now=now)
        except GuidedErrorException:
            self._session_store.create_session(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                state=initial_state,
                now=now,
            )

    def run(
        self,
        *,
        tenant_id: str,
        encounter_id: str,
        transcript_object_path: str,
        session_id: str,
        user_id: str,
        soap_extractor: SoapExtractorFn,
        transcript_version_id: str | None = None,
        conflict_reviewer: ConflictReviewerFn | None = None,
        trigger_conflict_review: bool = False,
        max_regenerations: int = DEFAULT_MAX_REGENERATIONS,
        max_argon_escalations: int = MAX_ARGON_ESCALATIONS,
        known_fhir_refs: Iterable[str] | None = None,
        ttl_seconds: int = 900,
        job_id: str | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> VisitDraftPipelineResult:
        """Execute bounded extraction, deterministic grounding verification, optional Argon review, and HITL pause."""
        if max_regenerations < 0:
            raise ValueError("max_regenerations must be >= 0")
        if max_argon_escalations < 0:
            raise ValueError("max_argon_escalations must be >= 0")

        resolved_job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        resolved_trace = trace_id or uuid.uuid4().hex
        ts = now or datetime.now(timezone.utc)

        self._ensure_session(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            initial_state={
                "tenant_id": tenant_id,
                "encounter_id": encounter_id,
                "job_id": resolved_job_id,
                "active_step": "fetching_transcript",
                "workflow_status": JobState.DRAFTING.value,
                "last_trace_id": resolved_trace,
            },
            now=ts,
        )

        # Stage 1a: Fetch verbatim transcript segments through CareThreadPolicyPlugin
        transcript_result: TranscriptSegmentsResult = self._tool_registry.invoke_with_policy(
            policy_plugin=self._policy_plugin,
            caller_id="soap_extractor_agent",
            tool_name="fetch_verbatim_transcript_segments",
            tool_kwargs={
                "tenant_id": tenant_id,
                "object_path": transcript_object_path,
                "version_id": transcript_version_id,
            },
            tenant_id=tenant_id,
            workflow="visit_draft_pipeline",
            job_id=resolved_job_id,
            trace_id=resolved_trace,
            encounter_id=encounter_id,
            now=ts,
        )

        self._session_store.update_state(
            session_id,
            {
                "transcript_ref": transcript_result.transcript_ref,
                "transcript_version_id": transcript_result.transcript_version_id,
                "active_step": "soap_extraction",
            },
            now=ts,
        )

        regeneration_count = 0
        argon_escalation_count = 0
        feedback: GuidedErrorEnvelope | None = None
        verified_draft: SoapDraftPayload | None = None
        validation_result: GroundingValidationResult | None = None

        for attempt in range(max_regenerations + 1):
            remaining_retries = max_regenerations - attempt
            route_cfg = self._model_router.route_extraction(
                response_schema=SoapDraftPayload,
                thinking_level="MEDIUM",
                task="soap_extraction",
            )
            prompt_text = _build_extraction_prompt(
                encounter_id=encounter_id,
                segments=transcript_result.segments,
                feedback=feedback,
            )

            self._policy_plugin.before_model_call(
                caller_id="soap_extractor_agent",
                model_id=route_cfg.model_id,
                prompt_text=prompt_text,
                has_untrusted_data=True,
                expected_labels=["verbatim_transcript"],
                max_input_tokens=route_cfg.max_input_tokens,
                tenant_id=tenant_id,
                workflow="visit_draft_pipeline",
                job_id=resolved_job_id,
                trace_id=resolved_trace,
                encounter_id=encounter_id,
            )

            candidate_draft = soap_extractor(
                prompt_text,
                transcript_result.segments,
                route_cfg,
                feedback,
                attempt,
            )

            # Stage 2: Deterministic EvidenceVerifier via validate_soap_draft_grounding
            grounding = self._tool_registry.invoke_with_policy(
                policy_plugin=self._policy_plugin,
                caller_id="evidence_verifier_agent",
                tool_name="validate_soap_draft_grounding",
                tool_kwargs={
                    "soap_draft": candidate_draft,
                    "transcript_segments": transcript_result.segments,
                    "known_fhir_refs": known_fhir_refs,
                    "max_retries_remaining": remaining_retries,
                    "trace_id": resolved_trace,
                },
                tenant_id=tenant_id,
                workflow="visit_draft_pipeline",
                job_id=resolved_job_id,
                trace_id=resolved_trace,
                encounter_id=encounter_id,
                now=ts,
            )

            if isinstance(grounding, GroundingValidationResult):
                verified_draft = (
                    candidate_draft
                    if isinstance(candidate_draft, SoapDraftPayload)
                    else SoapDraftPayload.model_validate(candidate_draft)
                )
                validation_result = grounding
                break

            feedback = grounding
            self._session_store.append_event(
                session_id,
                role="tool",
                content_summary=(
                    f"Grounding validation failed ({grounding.error_code}); "
                    f"retries_remaining={remaining_retries}"
                ),
                metadata={"error_code": grounding.error_code, "attempt": attempt},
                now=ts,
            )

            if not grounding.retryable or attempt >= max_regenerations:
                terminal_error = grounding.model_copy(update={"max_retries_remaining": 0})
                self._session_store.update_state(
                    session_id,
                    {
                        "active_step": "failed_terminal",
                        "workflow_status": JobState.FAILED_TERMINAL.value,
                        "last_trace_id": resolved_trace,
                    },
                    now=ts,
                )
                return VisitDraftPipelineResult(
                    job_id=resolved_job_id,
                    session_id=session_id,
                    status=JobState.FAILED_TERMINAL,
                    draft=None,
                    draft_sha256=None,
                    confirmation=None,
                    regeneration_count=regeneration_count,
                    argon_escalation_count=argon_escalation_count,
                    last_error=terminal_error,
                    trace_id=resolved_trace,
                )

            regeneration_count += 1

        assert verified_draft is not None and validation_result is not None

        # Stage 3: Optional eval-gated ConflictReviewer (max 1 Argon escalation)
        wants_conflict_review = trigger_conflict_review or _has_conflict_review_trigger(verified_draft)
        if (
            conflict_reviewer is not None
            and wants_conflict_review
            and argon_escalation_count < max_argon_escalations
            and self._model_router.can_escalate_to_argon(argon_escalation_count)
        ):
            argon_route = self._model_router.route_conflict_escalation(
                response_schema=SoapDraftPayload,
                escalation_count=argon_escalation_count,
                thinking_level="HIGH",
            )
            review_prompt = _build_extraction_prompt(
                encounter_id=encounter_id,
                segments=transcript_result.segments,
                feedback=None,
            )
            self._policy_plugin.before_model_call(
                caller_id="conflict_reviewer_agent",
                model_id=argon_route.model_id,
                prompt_text=review_prompt,
                has_untrusted_data=True,
                expected_labels=["verbatim_transcript"],
                max_input_tokens=argon_route.max_input_tokens,
                tenant_id=tenant_id,
                workflow="visit_draft_pipeline",
                job_id=resolved_job_id,
                trace_id=resolved_trace,
                encounter_id=encounter_id,
            )
            reviewed_candidate = conflict_reviewer(
                review_prompt,
                verified_draft,
                transcript_result.segments,
                argon_route,
            )
            if argon_route.escalated_to_argon:
                argon_escalation_count += 1

            recheck = self._tool_registry.invoke_with_policy(
                policy_plugin=self._policy_plugin,
                caller_id="evidence_verifier_agent",
                tool_name="validate_soap_draft_grounding",
                tool_kwargs={
                    "soap_draft": reviewed_candidate,
                    "transcript_segments": transcript_result.segments,
                    "known_fhir_refs": known_fhir_refs,
                    "max_retries_remaining": 0,
                    "trace_id": resolved_trace,
                },
                tenant_id=tenant_id,
                workflow="visit_draft_pipeline",
                job_id=resolved_job_id,
                trace_id=resolved_trace,
                encounter_id=encounter_id,
                now=ts,
            )
            if isinstance(recheck, GroundingValidationResult):
                verified_draft = (
                    reviewed_candidate
                    if isinstance(reviewed_candidate, SoapDraftPayload)
                    else SoapDraftPayload.model_validate(reviewed_candidate)
                )
                validation_result = recheck
            else:
                terminal_error = recheck.model_copy(update={"max_retries_remaining": 0})
                self._session_store.update_state(
                    session_id,
                    {
                        "active_step": "failed_terminal",
                        "workflow_status": JobState.FAILED_TERMINAL.value,
                        "last_trace_id": resolved_trace,
                    },
                    now=ts,
                )
                return VisitDraftPipelineResult(
                    job_id=resolved_job_id,
                    session_id=session_id,
                    status=JobState.FAILED_TERMINAL,
                    draft=None,
                    draft_sha256=None,
                    confirmation=None,
                    regeneration_count=regeneration_count,
                    argon_escalation_count=argon_escalation_count,
                    last_error=terminal_error,
                    trace_id=resolved_trace,
                )

        # Stage 4: Pause at durable digest-bound clinician sign-off HITL confirmation
        confirmation: HitlConfirmation = self._tool_registry.invoke_with_policy(
            policy_plugin=self._policy_plugin,
            caller_id="adk_coordinator",
            tool_name="request_clinician_signoff_confirmation",
            tool_kwargs={
                "draft_id": verified_draft.draft_id,
                "draft_version": verified_draft.draft_version,
                "draft_sha256": validation_result.draft_sha256,
                "ttl_seconds": ttl_seconds,
                "now": ts,
            },
            tenant_id=tenant_id,
            workflow="visit_draft_pipeline",
            job_id=resolved_job_id,
            trace_id=resolved_trace,
            encounter_id=encounter_id,
            now=ts,
        )

        # Persist ONLY filtered non-PHI workflow pointers and digests
        self._session_store.update_state(
            session_id,
            {
                "draft_id": verified_draft.draft_id,
                "draft_version": verified_draft.draft_version,
                "draft_sha256": validation_result.draft_sha256,
                "speaker_attribution_status": verified_draft.speaker_attribution.status,
                "confirmation_id": confirmation.confirmation_id,
                "active_step": "awaiting_clinician_signoff",
                "workflow_status": JobState.AWAITING_CLINICIAN_REVIEW.value,
                "last_trace_id": resolved_trace,
            },
            now=ts,
        )
        self._session_store.append_event(
            session_id,
            role="tool",
            content_summary=f"Created pending clinician signoff confirmation {confirmation.confirmation_id}",
            function_call_id=confirmation.confirmation_id,
            is_function_call=True,
            requested_tool_confirmations={
                confirmation.confirmation_id: {
                    "artifact_id": verified_draft.draft_id,
                    "artifact_version": verified_draft.draft_version,
                    "sha256_digest": validation_result.draft_sha256,
                    "kind": confirmation.kind,
                }
            },
            now=ts,
        )

        return VisitDraftPipelineResult(
            job_id=resolved_job_id,
            session_id=session_id,
            status=JobState.AWAITING_CLINICIAN_REVIEW,
            draft=verified_draft,
            draft_sha256=validation_result.draft_sha256,
            confirmation=confirmation,
            regeneration_count=regeneration_count,
            argon_escalation_count=argon_escalation_count,
            last_error=None,
            trace_id=resolved_trace,
        )


class CareThreadCoordinator:
    """Deterministic ADK Coordinator orchestrating visit drafting, continuity briefs, and insurer release."""

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry,
        policy_plugin: CareThreadPolicyPlugin,
        model_router: ModelRouter,
        session_store: FilteredSessionStore,
        authz_service: AuthzService,
        continuity_service: ContinuityService,
    ) -> None:
        self._tool_registry = tool_registry
        self._policy_plugin = policy_plugin
        self._model_router = model_router
        self._session_store = session_store
        self._authz_service = authz_service
        self._continuity_service = continuity_service
        self._visit_pipeline = VisitDraftSequentialPipeline(
            tool_registry=tool_registry,
            policy_plugin=policy_plugin,
            model_router=model_router,
            session_store=session_store,
        )

    def _ensure_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        initial_state: dict[str, Any],
        now: datetime,
    ) -> None:
        try:
            self._session_store.get_session(session_id)
            self._session_store.update_state(session_id, initial_state, now=now)
        except GuidedErrorException:
            self._session_store.create_session(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                state=initial_state,
                now=now,
            )

    def run_visit_draft_pipeline(
        self,
        *,
        tenant_id: str,
        encounter_id: str,
        transcript_object_path: str,
        session_id: str,
        user_id: str,
        soap_extractor: SoapExtractorFn,
        transcript_version_id: str | None = None,
        conflict_reviewer: ConflictReviewerFn | None = None,
        trigger_conflict_review: bool = False,
        max_regenerations: int = DEFAULT_MAX_REGENERATIONS,
        max_argon_escalations: int = MAX_ARGON_ESCALATIONS,
        known_fhir_refs: Iterable[str] | None = None,
        ttl_seconds: int = 900,
        job_id: str | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> VisitDraftPipelineResult:
        """Run the bounded sequential VisitDraft pipeline and pause at clinician sign-off HITL."""
        return self._visit_pipeline.run(
            tenant_id=tenant_id,
            encounter_id=encounter_id,
            transcript_object_path=transcript_object_path,
            session_id=session_id,
            user_id=user_id,
            soap_extractor=soap_extractor,
            transcript_version_id=transcript_version_id,
            conflict_reviewer=conflict_reviewer,
            trigger_conflict_review=trigger_conflict_review,
            max_regenerations=max_regenerations,
            max_argon_escalations=max_argon_escalations,
            known_fhir_refs=known_fhir_refs,
            ttl_seconds=ttl_seconds,
            job_id=job_id,
            trace_id=trace_id,
            now=now,
        )

    def run_continuity_brief_workflow(
        self,
        *,
        actor: ActorContext,
        patient_id: str,
        patient_tenant_id: str,
        consent_record: ConsentRecord,
        current_encounter: CurrentEncounterContext,
        as_of_date: date,
        session_id: str,
        max_encounters: int = 10,
        requested_categories: Iterable[str] | None = None,
        job_id: str | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> ContinuityBriefPayload:
        """Authorize access, query Spanner Graph, verify FHIR versions, fallback if needed, and build ContinuityBrief."""
        ts = now or datetime.now(timezone.utc)
        resolved_job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        resolved_trace = trace_id or uuid.uuid4().hex

        scope_token = self._authz_service.authorize_clinical_access(
            actor=actor,
            patient_id=patient_id,
            patient_tenant_id=patient_tenant_id,
            consent_record=consent_record,
            now=ts,
            requested_categories=requested_categories,
            trace_id=resolved_trace,
        )

        self._ensure_session(
            session_id=session_id,
            tenant_id=patient_tenant_id,
            user_id=actor.actor_id,
            initial_state={
                "tenant_id": patient_tenant_id,
                "encounter_id": current_encounter.encounter_id,
                "job_id": resolved_job_id,
                "active_step": "continuity_retrieval",
                "workflow_status": "RUNNING",
                "last_trace_id": resolved_trace,
            },
            now=ts,
        )

        verified_result: VerifiedContinuityResult | None = None
        fallback_reason: str | None = None
        staleness_banner: str | None = None

        try:
            snapshot = self._tool_registry.invoke_with_policy(
                policy_plugin=self._policy_plugin,
                caller_id="continuity_retrieval_agent",
                tool_name="query_patient_continuity_graph",
                tool_kwargs={
                    "scope_token": scope_token,
                    "as_of_date": as_of_date,
                    "max_encounters": max_encounters,
                    "max_hops": MAX_HOPS_LIMIT,
                },
                tenant_id=patient_tenant_id,
                workflow="continuity_brief_workflow",
                job_id=resolved_job_id,
                trace_id=resolved_trace,
                scope_token=scope_token,
                patient_id=patient_id,
                actor_id=actor.actor_id,
                encounter_id=current_encounter.encounter_id,
                now=ts,
            )
        except (SpannerGraphTimeoutError, TimeoutError):
            snapshot = None
            fallback_reason = "SPANNER_GRAPH_TIMEOUT"
            staleness_banner = "Derived graph index timed out; served directly from authoritative FHIR R4."
        except (SpannerGraphUnavailableError, RuntimeError):
            snapshot = None
            fallback_reason = "SPANNER_GRAPH_ERROR"
            staleness_banner = "Derived graph index unavailable; served directly from authoritative FHIR R4."

        if snapshot is not None:
            filtered_encounters = [
                enc
                for enc in snapshot.encounters
                if enc.encounter_id != current_encounter.encounter_id
            ]
            filtered_enc_ids = {enc.encounter_id for enc in filtered_encounters}
            filtered_meds = [
                m for m in snapshot.medication_events if m.encounter_id in filtered_enc_ids
            ]
            filtered_probs = [
                p for p in snapshot.problem_events if p.encounter_id in filtered_enc_ids
            ]
            all_refs = _collect_fhir_version_refs(
                filtered_encounters, filtered_meds, filtered_probs
            )

            if not filtered_encounters:
                fallback_reason = "MISSING_GRAPH_PROJECTION"
                staleness_banner = (
                    "Derived graph projection missing; served directly from authoritative FHIR R4."
                )
            else:
                ver_result: FhirVersionVerificationResult = self._tool_registry.invoke_with_policy(
                    policy_plugin=self._policy_plugin,
                    caller_id="continuity_retrieval_agent",
                    tool_name="verify_fhir_resource_versions",
                    tool_kwargs={
                        "scope_token": scope_token,
                        "fhir_version_refs": all_refs,
                    },
                    tenant_id=patient_tenant_id,
                    workflow="continuity_brief_workflow",
                    job_id=resolved_job_id,
                    trace_id=resolved_trace,
                    scope_token=scope_token,
                    patient_id=patient_id,
                    actor_id=actor.actor_id,
                    encounter_id=current_encounter.encounter_id,
                    now=ts,
                )
                if ver_result.all_fresh:
                    allowed_refs = _collect_fhir_version_refs(
                        filtered_encounters,
                        filtered_meds,
                        filtered_probs,
                        allowed_categories=scope_token.allowed_categories,
                    )
                    prior_items = _snapshot_to_prior_items(
                        snapshot_encounters=filtered_encounters,
                        med_events=filtered_meds,
                        problem_events=filtered_probs,
                        practitioners=snapshot.practitioners,
                        allowed_categories=scope_token.allowed_categories,
                    )
                    verified_result = VerifiedContinuityResult(
                        tenant_id=patient_tenant_id,
                        patient_id=patient_id,
                        as_of_date=as_of_date,
                        continuity_source=ContinuitySource.SPANNER_GRAPH_VERIFIED,
                        staleness_banner=None,
                        fallback_reason=None,
                        graph_watermark_ts=snapshot.watermark_ts,
                        encounters=filtered_encounters,
                        prior_history_items=prior_items,
                        fhir_version_refs=allowed_refs,
                    )
                else:
                    fallback_reason = "STALE_FHIR_VERSION"
                    staleness_banner = (
                        "Derived graph index version mismatch detected; "
                        "served directly from authoritative FHIR R4."
                    )

        if verified_result is None:
            verified_result = self._tool_registry.invoke_with_policy(
                policy_plugin=self._policy_plugin,
                caller_id="continuity_retrieval_agent",
                tool_name="fetch_authoritative_fhir_history",
                tool_kwargs={
                    "scope_token": scope_token,
                    "as_of_date": as_of_date,
                    "max_encounters": max_encounters,
                    "current_encounter_id": current_encounter.encounter_id,
                    "fallback_reason": fallback_reason or "AUTHORITATIVE_FHIR_FALLBACK",
                    "staleness_banner": staleness_banner
                    or "Served directly from authoritative FHIR R4.",
                    "now": ts,
                },
                tenant_id=patient_tenant_id,
                workflow="continuity_brief_workflow",
                job_id=resolved_job_id,
                trace_id=resolved_trace,
                scope_token=scope_token,
                patient_id=patient_id,
                actor_id=actor.actor_id,
                encounter_id=current_encounter.encounter_id,
                now=ts,
            )

        brief = verified_result.to_continuity_brief(current_encounter)
        prior_ref_strings = [
            f"{ref.resource_type}/{ref.resource_id}/_history/{ref.version_id}"
            for ref in brief.fhir_version_refs
        ]
        self._session_store.update_state(
            session_id,
            {
                "fhir_prior_refs": prior_ref_strings,
                "active_step": "continuity_brief_ready",
                "workflow_status": "COMPLETED",
                "last_trace_id": resolved_trace,
            },
            now=ts,
        )
        return brief

    def run_insurer_packet_workflow(
        self,
        *,
        grant: ReleaseGrant,
        consent_record: ConsentRecord,
        checklist_requirements: Sequence[str | ChecklistRequirement],
        session_id: str,
        user_id: str = "records-reviewer",
        packet_id: str | None = None,
        packet_version: int = 1,
        ttl_seconds: int = 900,
        job_id: str | None = None,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> InsurerPacketWorkflowResult:
        """Assemble allowlisted signed-FHIR insurer packet and pause at durable HITL release confirmation."""
        ts = now or datetime.now(timezone.utc)
        resolved_job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
        resolved_trace = trace_id or uuid.uuid4().hex

        self._ensure_session(
            session_id=session_id,
            tenant_id=grant.tenant_id,
            user_id=user_id,
            initial_state={
                "tenant_id": grant.tenant_id,
                "job_id": resolved_job_id,
                "active_step": "assembling_insurer_packet",
                "workflow_status": "RUNNING",
                "last_trace_id": resolved_trace,
            },
            now=ts,
        )

        packet: InsurerPacketPayload = self._tool_registry.invoke_with_policy(
            policy_plugin=self._policy_plugin,
            caller_id="records_release_coordinator",
            tool_name="assemble_allowlisted_insurer_packet",
            tool_kwargs={
                "grant": grant,
                "consent": consent_record,
                "checklist_requirements": checklist_requirements,
                "now": ts,
                "packet_id": packet_id,
                "packet_version": packet_version,
            },
            tenant_id=grant.tenant_id,
            workflow="insurer_packet_workflow",
            job_id=resolved_job_id,
            trace_id=resolved_trace,
            now=ts,
        )

        assert packet.packet_sha256 is not None
        confirmation: HitlConfirmation = self._tool_registry.invoke_with_policy(
            policy_plugin=self._policy_plugin,
            caller_id="records_release_coordinator",
            tool_name="request_release_hitl_confirmation",
            tool_kwargs={
                "packet_id": packet.packet_id,
                "packet_version": packet.packet_version,
                "packet_sha256": packet.packet_sha256,
                "ttl_seconds": ttl_seconds,
                "now": ts,
            },
            tenant_id=grant.tenant_id,
            workflow="insurer_packet_workflow",
            job_id=resolved_job_id,
            trace_id=resolved_trace,
            now=ts,
        )

        self._session_store.update_state(
            session_id,
            {
                "artifact_id": packet.packet_id,
                "artifact_version": packet.packet_version,
                "artifact_sha256": packet.packet_sha256,
                "confirmation_id": confirmation.confirmation_id,
                "active_step": "awaiting_release_confirmation",
                "workflow_status": "AWAITING_HUMAN_RELEASE",
                "last_trace_id": resolved_trace,
            },
            now=ts,
        )
        self._session_store.append_event(
            session_id,
            role="tool",
            content_summary=f"Created pending insurer release confirmation {confirmation.confirmation_id}",
            function_call_id=confirmation.confirmation_id,
            is_function_call=True,
            requested_tool_confirmations={
                confirmation.confirmation_id: {
                    "artifact_id": packet.packet_id,
                    "artifact_version": packet.packet_version,
                    "sha256_digest": packet.packet_sha256,
                    "kind": confirmation.kind,
                }
            },
            now=ts,
        )

        return InsurerPacketWorkflowResult(
            job_id=resolved_job_id,
            session_id=session_id,
            status="AWAITING_HUMAN_RELEASE",
            packet=packet,
            confirmation=confirmation,
            trace_id=resolved_trace,
        )


__all__ = [
    "CareThreadCoordinator",
    "ConflictReviewerFn",
    "DEFAULT_MAX_REGENERATIONS",
    "InsurerPacketWorkflowResult",
    "SoapExtractorFn",
    "VisitDraftPipelineResult",
    "VisitDraftSequentialPipeline",
]
