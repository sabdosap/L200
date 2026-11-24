"""Bounded continuity retrieval service with FHIR R4 version verification and automatic fallback."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
import uuid

from pydantic import Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel
from carethread.schemas.v1.continuity import (
    ContinuityBriefPayload,
    ContinuitySource,
    CurrentEncounterContext,
    FhirVersionRef,
    PriorHistoryItem,
)
from carethread.security.authz import AuthzService, ScopeCapabilityToken
from carethread.stores.fhir import FhirR4Store, FhirResourceNotFoundError
from carethread.stores.spanner_graph import (
    MAX_ENCOUNTERS_LIMIT,
    MAX_HOPS_LIMIT,
    EncounterNode,
    InMemorySpannerGraphStore,
    SpannerGraphStore,
    SpannerGraphTimeoutError,
    SpannerGraphUnavailableError,
)


class VerifiedContinuityResult(StrictModel):
    """Verified longitudinal continuity history with explicit source provenance."""

    tenant_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    as_of_date: date
    continuity_source: ContinuitySource
    staleness_banner: str | None = None
    fallback_reason: str | None = None
    graph_watermark_ts: datetime | None = None
    encounters: list[EncounterNode] = Field(default_factory=list)
    prior_history_items: list[PriorHistoryItem] = Field(default_factory=list)
    fhir_version_refs: list[FhirVersionRef] = Field(default_factory=list)

    def to_continuity_brief(
        self,
        current_encounter: CurrentEncounterContext,
    ) -> ContinuityBriefPayload:
        """Assemble a schema-validated ContinuityBriefPayload separated from current_encounter."""
        current_id = current_encounter.encounter_id
        excluded_refs: set[tuple[str, str, str]] = {
            ("Encounter", current_id, enc.fhir_version_id)
            for enc in self.encounters
            if enc.encounter_id == current_id and enc.fhir_version_id
        }
        for enc in self.encounters:
            if enc.encounter_id == current_id and enc.composition_id and enc.composition_version_id:
                excluded_refs.add(("Composition", enc.composition_id, enc.composition_version_id))

        filtered_items: list[PriorHistoryItem] = []
        for item in self.prior_history_items:
            if item.encounter_id == current_id:
                if item.fhir_ref is not None:
                    excluded_refs.add(
                        (
                            item.fhir_ref.resource_type,
                            item.fhir_ref.resource_id,
                            item.fhir_ref.version_id,
                        )
                    )
                continue
            filtered_items.append(item)

        filtered_refs = [
            ref
            for ref in self.fhir_version_refs
            if not (ref.resource_type == "Encounter" and ref.resource_id == current_id)
            and (ref.resource_type, ref.resource_id, ref.version_id) not in excluded_refs
        ]
        return ContinuityBriefPayload(
            patient_id=self.patient_id,
            continuity_source=self.continuity_source,
            staleness_banner=self.staleness_banner,
            graph_watermark_ts=self.graph_watermark_ts,
            prior_history_items=filtered_items,
            current_encounter=current_encounter,
            fhir_version_refs=filtered_refs,
        )


_CONTINUITY_CATEGORIES: frozenset[str] = frozenset(
    {"clinical_summary", "medications", "conditions"}
)


def _collect_fhir_version_refs(
    encounters: list[EncounterNode],
    med_events: list[Any],
    problem_events: list[Any],
    allowed_categories: frozenset[str] | None = None,
) -> list[FhirVersionRef]:
    allow_summary = allowed_categories is None or "clinical_summary" in allowed_categories
    allow_meds = allowed_categories is None or "medications" in allowed_categories
    allow_conditions = allowed_categories is None or "conditions" in allowed_categories
    allow_any_clinical = allowed_categories is None or bool(
        allowed_categories & _CONTINUITY_CATEGORIES
    )

    ref_map: dict[tuple[str, str], FhirVersionRef] = {}
    if allow_any_clinical:
        for enc in encounters:
            if enc.fhir_version_id:
                ref_map[("Encounter", enc.encounter_id)] = FhirVersionRef(
                    resource_type="Encounter",
                    resource_id=enc.encounter_id,
                    version_id=enc.fhir_version_id,
                )
            if allow_summary and enc.composition_id and enc.composition_version_id:
                ref_map[("Composition", enc.composition_id)] = FhirVersionRef(
                    resource_type="Composition",
                    resource_id=enc.composition_id,
                    version_id=enc.composition_version_id,
                )
    if allow_meds:
        for med in med_events:
            ref_map[(med.fhir_resource_type, med.fhir_resource_id)] = FhirVersionRef(
                resource_type=med.fhir_resource_type,
                resource_id=med.fhir_resource_id,
                version_id=med.fhir_version_id,
            )
    if allow_conditions:
        for prob in problem_events:
            ref_map[(prob.fhir_resource_type, prob.fhir_resource_id)] = FhirVersionRef(
                resource_type=prob.fhir_resource_type,
                resource_id=prob.fhir_resource_id,
                version_id=prob.fhir_version_id,
            )
    return sorted(
        ref_map.values(),
        key=lambda r: (r.resource_type, r.resource_id, r.version_id),
    )


def _snapshot_to_prior_items(
    snapshot_encounters: list[EncounterNode],
    med_events: list[Any],
    problem_events: list[Any],
    practitioners: dict[str, Any],
    allowed_categories: frozenset[str],
) -> list[PriorHistoryItem]:
    enc_by_id = {enc.encounter_id: enc for enc in snapshot_encounters}
    items: list[PriorHistoryItem] = []

    if "clinical_summary" in allowed_categories:
        for enc in snapshot_encounters:
            prac = practitioners.get(enc.practitioner_id or "")
            prac_name = prac.display_name if prac is not None else None
            if enc.composition_id and enc.composition_version_id:
                items.append(
                    PriorHistoryItem(
                        item_id=f"COMP-{enc.composition_id}",
                        encounter_id=enc.encounter_id,
                        encounter_date=enc.encounter_date,
                        specialty=enc.specialty,
                        practitioner_name=prac_name,
                        category="clinical_note",
                        summary=enc.composition_title or f"Signed SOAP Note - {enc.encounter_id}",
                        evidence_ids=[
                            f"Composition/{enc.composition_id}/_history/{enc.composition_version_id}"
                        ],
                        fhir_ref=FhirVersionRef(
                            resource_type="Composition",
                            resource_id=enc.composition_id,
                            version_id=enc.composition_version_id,
                        ),
                    )
                )

    if "medications" in allowed_categories:
        for med in med_events:
            enc = enc_by_id.get(med.encounter_id)
            prac = practitioners.get(enc.practitioner_id or "") if enc else None
            items.append(
                PriorHistoryItem(
                    item_id=f"MED-{med.fhir_resource_id}",
                    encounter_id=med.encounter_id,
                    encounter_date=med.encounter_date,
                    specialty=enc.specialty if enc else "General",
                    practitioner_name=prac.display_name if prac else None,
                    category="medication",
                    summary=med.summary,
                    medication_status=med.medication_status,
                    evidence_ids=list(med.evidence_ids),
                    fhir_ref=FhirVersionRef(
                        resource_type=med.fhir_resource_type,
                        resource_id=med.fhir_resource_id,
                        version_id=med.fhir_version_id,
                    ),
                )
            )

    if "conditions" in allowed_categories:
        for prob in problem_events:
            enc = enc_by_id.get(prob.encounter_id)
            prac = practitioners.get(enc.practitioner_id or "") if enc else None
            items.append(
                PriorHistoryItem(
                    item_id=f"PROB-{prob.fhir_resource_id}",
                    encounter_id=prob.encounter_id,
                    encounter_date=prob.encounter_date,
                    specialty=enc.specialty if enc else "General",
                    practitioner_name=prac.display_name if prac else None,
                    category="problem",
                    summary=prob.statement,
                    polarity=prob.polarity,
                    certainty=prob.certainty,
                    evidence_ids=list(prob.evidence_ids),
                    fhir_ref=FhirVersionRef(
                        resource_type=prob.fhir_resource_type,
                        resource_id=prob.fhir_resource_id,
                        version_id=prob.fhir_version_id,
                    ),
                )
            )

    return items


class ContinuityService:
    """Retrieves verified patient continuity with automatic authoritative FHIR R4 fallback."""

    def __init__(
        self,
        fhir_store: FhirR4Store,
        graph_store: SpannerGraphStore,
        authz_service: AuthzService,
    ) -> None:
        self._fhir_store = fhir_store
        self._graph_store = graph_store
        self._authz_service = authz_service

    def get_verified_patient_history(
        self,
        scope_token: ScopeCapabilityToken,
        as_of_date: date,
        max_encounters: int = 10,
        now: datetime | None = None,
        current_encounter_id: str | None = None,
    ) -> VerifiedContinuityResult:
        """Verify ScopeCapabilityToken, query Spanner Graph, verify FHIR versions, and fallback on staleness/error."""
        check_now = now or datetime.now(timezone.utc)
        if not self._authz_service.verify_token(scope_token, check_now):
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="INVALID_OR_EXPIRED_SCOPE_TOKEN",
                    category=ErrorCategory.AUTHZ_DENIED,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="scope_token",
                    guided_recovery="Re-authorize clinical access to obtain a valid ScopeCapabilityToken.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        if max_encounters < 1 or max_encounters > MAX_ENCOUNTERS_LIMIT:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="CONTINUITY_MAX_ENCOUNTERS_OUT_OF_BOUNDS",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="max_encounters",
                    guided_recovery=f"Request between 1 and {MAX_ENCOUNTERS_LIMIT} encounters.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        tenant_id = scope_token.tenant_id
        patient_id = scope_token.patient_id
        allowed_categories = scope_token.allowed_categories

        # Fail closed when token grants no clinical continuity categories.
        if not (allowed_categories & _CONTINUITY_CATEGORIES):
            return VerifiedContinuityResult(
                tenant_id=tenant_id,
                patient_id=patient_id,
                as_of_date=as_of_date,
                continuity_source=ContinuitySource.SPANNER_GRAPH_VERIFIED,
                staleness_banner=None,
                fallback_reason=None,
                graph_watermark_ts=None,
                encounters=[],
                prior_history_items=[],
                fhir_version_refs=[],
            )

        try:
            snapshot = self._graph_store.query_patient_continuity(
                tenant_id=tenant_id,
                patient_id=patient_id,
                max_encounters=max_encounters,
                max_hops=MAX_HOPS_LIMIT,
                as_of_date=as_of_date,
            )
        except (SpannerGraphTimeoutError, TimeoutError):
            return self._fallback_to_authoritative_fhir(
                tenant_id=tenant_id,
                patient_id=patient_id,
                as_of_date=as_of_date,
                max_encounters=max_encounters,
                current_encounter_id=current_encounter_id,
                allowed_categories=allowed_categories,
                fallback_reason="SPANNER_GRAPH_TIMEOUT",
                staleness_banner="Derived graph index timed out; served directly from authoritative FHIR R4.",
            )
        except (SpannerGraphUnavailableError, RuntimeError):
            return self._fallback_to_authoritative_fhir(
                tenant_id=tenant_id,
                patient_id=patient_id,
                as_of_date=as_of_date,
                max_encounters=max_encounters,
                current_encounter_id=current_encounter_id,
                allowed_categories=allowed_categories,
                fallback_reason="SPANNER_GRAPH_ERROR",
                staleness_banner="Derived graph index unavailable; served directly from authoritative FHIR R4.",
            )

        filtered_encounters = [
            enc
            for enc in snapshot.encounters
            if current_encounter_id is None or enc.encounter_id != current_encounter_id
        ]
        filtered_enc_ids = {enc.encounter_id for enc in filtered_encounters}
        filtered_meds = [m for m in snapshot.medication_events if m.encounter_id in filtered_enc_ids]
        filtered_probs = [p for p in snapshot.problem_events if p.encounter_id in filtered_enc_ids]
        all_snapshot_refs = _collect_fhir_version_refs(
            filtered_encounters, filtered_meds, filtered_probs
        )

        # If graph has no encounters for this patient, verify authoritative FHIR is also empty.
        if not filtered_encounters:
            authoritative = self._build_authoritative_snapshot(
                tenant_id=tenant_id,
                patient_id=patient_id,
                as_of_date=as_of_date,
                max_encounters=max_encounters,
                current_encounter_id=current_encounter_id,
                allowed_categories=allowed_categories,
            )
            if authoritative.encounters:
                return self._result_from_authoritative_snapshot(
                    authoritative=authoritative,
                    as_of_date=as_of_date,
                    allowed_categories=allowed_categories,
                    fallback_reason="MISSING_GRAPH_PROJECTION",
                    staleness_banner="Derived graph projection missing; served directly from authoritative FHIR R4.",
                )

        # Verify EVERY returned fhir_version_ref against authoritative FHIR R4.
        if not self._verify_all_fhir_versions(tenant_id, all_snapshot_refs):
            return self._fallback_to_authoritative_fhir(
                tenant_id=tenant_id,
                patient_id=patient_id,
                as_of_date=as_of_date,
                max_encounters=max_encounters,
                current_encounter_id=current_encounter_id,
                allowed_categories=allowed_categories,
                fallback_reason="STALE_FHIR_VERSION",
                staleness_banner="Derived graph index version mismatch detected; served directly from authoritative FHIR R4.",
            )

        allowed_refs = _collect_fhir_version_refs(
            filtered_encounters,
            filtered_meds,
            filtered_probs,
            allowed_categories=allowed_categories,
        )
        prior_items = _snapshot_to_prior_items(
            snapshot_encounters=filtered_encounters,
            med_events=filtered_meds,
            problem_events=filtered_probs,
            practitioners=snapshot.practitioners,
            allowed_categories=allowed_categories,
        )

        return VerifiedContinuityResult(
            tenant_id=tenant_id,
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

    def fetch_authoritative_history(
        self,
        scope_token: ScopeCapabilityToken,
        as_of_date: date,
        max_encounters: int = 10,
        *,
        current_encounter_id: str | None = None,
        fallback_reason: str = "AUTHORITATIVE_FHIR_DIRECT_READ",
        staleness_banner: str = "Served directly from authoritative FHIR R4.",
        now: datetime | None = None,
    ) -> VerifiedContinuityResult:
        """Verify ScopeCapabilityToken and read patient continuity directly from authoritative FHIR R4."""
        check_now = now or datetime.now(timezone.utc)
        if not self._authz_service.verify_token(scope_token, check_now):
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="INVALID_OR_EXPIRED_SCOPE_TOKEN",
                    category=ErrorCategory.AUTHZ_DENIED,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="scope_token",
                    guided_recovery="Re-authorize clinical access to obtain a valid ScopeCapabilityToken.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        if max_encounters < 1 or max_encounters > MAX_ENCOUNTERS_LIMIT:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="CONTINUITY_MAX_ENCOUNTERS_OUT_OF_BOUNDS",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="max_encounters",
                    guided_recovery=f"Request between 1 and {MAX_ENCOUNTERS_LIMIT} encounters.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        return self._fallback_to_authoritative_fhir(
            tenant_id=scope_token.tenant_id,
            patient_id=scope_token.patient_id,
            as_of_date=as_of_date,
            max_encounters=max_encounters,
            current_encounter_id=current_encounter_id,
            allowed_categories=scope_token.allowed_categories,
            fallback_reason=fallback_reason,
            staleness_banner=staleness_banner,
        )

    def _verify_all_fhir_versions(
        self,
        tenant_id: str,
        refs: list[FhirVersionRef],
    ) -> bool:
        for ref in refs:
            try:
                latest = self._fhir_store.read(tenant_id, ref.resource_type, ref.resource_id)
            except FhirResourceNotFoundError:
                return False
            latest_version = str((latest.get("meta") or {}).get("versionId", ""))
            if latest_version != ref.version_id:
                return False
        return True

    def _build_authoritative_snapshot(
        self,
        *,
        tenant_id: str,
        patient_id: str,
        as_of_date: date,
        max_encounters: int,
        current_encounter_id: str | None,
        allowed_categories: frozenset[str],
    ) -> Any:
        # Project directly from authoritative FHIR R4 in an ephemeral in-memory view.
        ephemeral = InMemorySpannerGraphStore()
        ephemeral.rebuild_from_fhir(tenant_id, self._fhir_store)
        snapshot = ephemeral.query_patient_continuity(
            tenant_id=tenant_id,
            patient_id=patient_id,
            max_encounters=max_encounters,
            max_hops=MAX_HOPS_LIMIT,
            as_of_date=as_of_date,
        )
        kept_encounters = [
            enc
            for enc in snapshot.encounters
            if current_encounter_id is None or enc.encounter_id != current_encounter_id
        ]
        kept_ids = {enc.encounter_id for enc in kept_encounters}
        kept_meds = [m for m in snapshot.medication_events if m.encounter_id in kept_ids]
        kept_probs = [p for p in snapshot.problem_events if p.encounter_id in kept_ids]
        return snapshot.model_copy(
            update={
                "encounters": kept_encounters,
                "medication_events": kept_meds,
                "problem_events": kept_probs,
                "fhir_version_refs": _collect_fhir_version_refs(
                    kept_encounters,
                    kept_meds,
                    kept_probs,
                    allowed_categories=allowed_categories,
                ),
            }
        )

    def _result_from_authoritative_snapshot(
        self,
        *,
        authoritative: Any,
        as_of_date: date,
        allowed_categories: frozenset[str],
        fallback_reason: str,
        staleness_banner: str,
    ) -> VerifiedContinuityResult:
        prior_items = _snapshot_to_prior_items(
            snapshot_encounters=authoritative.encounters,
            med_events=authoritative.medication_events,
            problem_events=authoritative.problem_events,
            practitioners=authoritative.practitioners,
            allowed_categories=allowed_categories,
        )
        return VerifiedContinuityResult(
            tenant_id=authoritative.tenant_id,
            patient_id=authoritative.patient_id,
            as_of_date=as_of_date,
            continuity_source=ContinuitySource.FHIR_R4_AUTHORITATIVE,
            staleness_banner=staleness_banner,
            fallback_reason=fallback_reason,
            graph_watermark_ts=None,
            encounters=authoritative.encounters,
            prior_history_items=prior_items,
            fhir_version_refs=authoritative.fhir_version_refs,
        )

    def _fallback_to_authoritative_fhir(
        self,
        *,
        tenant_id: str,
        patient_id: str,
        as_of_date: date,
        max_encounters: int,
        current_encounter_id: str | None,
        allowed_categories: frozenset[str],
        fallback_reason: str,
        staleness_banner: str,
    ) -> VerifiedContinuityResult:
        authoritative = self._build_authoritative_snapshot(
            tenant_id=tenant_id,
            patient_id=patient_id,
            as_of_date=as_of_date,
            max_encounters=max_encounters,
            current_encounter_id=current_encounter_id,
            allowed_categories=allowed_categories,
        )
        return self._result_from_authoritative_snapshot(
            authoritative=authoritative,
            as_of_date=as_of_date,
            allowed_categories=allowed_categories,
            fallback_reason=fallback_reason,
            staleness_banner=staleness_banner,
        )


__all__ = [
    "ContinuityService",
    "VerifiedContinuityResult",
]
