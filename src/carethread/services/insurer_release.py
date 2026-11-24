"""Allowlist-only synthetic insurer prior-authorization packet assembler."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
import re
from typing import Any
import uuid

from pydantic import Field

from carethread.schemas import StrictModel
from carethread.schemas.v1.insurer import (
    AllowlistedPacketItem,
    CoverageChecklist,
    InsurerPacketPayload,
)
from carethread.schemas.v1.soap import compute_canonical_sha256
from carethread.security.consent import ConsentRecord, GrantService, ReleaseGrant
from carethread.stores.fhir import FhirR4Store

ALLOWLISTED_FHIR_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "Composition",
        "Condition",
        "MedicationRequest",
        "MedicationStatement",
        "Observation",
        "DiagnosticReport",
        "DocumentReference",
        "Provenance",
    }
)

_RESOURCE_TYPE_TO_CATEGORY: dict[str, str] = {
    "Composition": "clinical_summary",
    "Condition": "conditions",
    "MedicationRequest": "medications",
    "MedicationStatement": "medications",
    "Observation": "observations",
    "DiagnosticReport": "diagnostic_reports",
    "DocumentReference": "diagnostic_reports",
    "Provenance": "provenance",
}

_RESOURCE_TYPE_SYNONYMS: dict[str, frozenset[str]] = {
    "Composition": frozenset({"note", "soap", "consultation", "summary", "composition"}),
    "Condition": frozenset({"condition", "diagnosis", "problem", "assessment"}),
    "MedicationRequest": frozenset({"medication", "medications", "prescription", "drug", "order"}),
    "MedicationStatement": frozenset({"medication", "medications", "history", "drug"}),
    "Observation": frozenset({"observation", "exam", "finding", "therapy", "plan"}),
    "DiagnosticReport": frozenset({"report", "diagnostic", "imaging", "lab", "study"}),
    "DocumentReference": frozenset({"document", "report", "imaging", "study"}),
    "Provenance": frozenset({"provenance", "signature", "attestation"}),
}

_FORBIDDEN_STATUSES: frozenset[str] = frozenset(
    {"preliminary", "draft", "entered-in-error", "cancelled", "unknown-draft"}
)

_REQUIRE_FINAL_STATUS_TYPES: frozenset[str] = frozenset(
    {"Composition", "Observation", "DiagnosticReport"}
)

_FORBIDDEN_CONTENT_MARKERS: tuple[str, ...] = (
    "audio/wav",
    "audio/mpeg",
    "audio/mp3",
    "raw_audio",
    "session_state",
    "health_score",
    "draft_awaiting_clinician_review",
)

_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with"}
)


class ChecklistRequirement(StrictModel):
    """Structured prior-authorization checklist requirement for deterministic matching."""

    label: str = Field(min_length=1)
    required_resource_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) > 1 and t not in _STOPWORDS}


def _extract_ref_id(ref: str | None, expected_type: str | None = None) -> str | None:
    if not ref or not isinstance(ref, str):
        return None
    base = ref.split("/_history/", 1)[0]
    if "/" in base:
        rtype, rid = base.split("/", 1)
        if expected_type is not None and rtype != expected_type:
            return None
        return rid
    return base if expected_type is None else None


def _parse_date(raw: str | None) -> date | None:
    if raw and isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip()[:10])
        except ValueError:
            return None
    return None


def _extract_document_reference_encounter_id(resource: dict[str, Any]) -> str | None:
    direct = _extract_ref_id((resource.get("encounter") or {}).get("reference"), "Encounter")
    if direct:
        return direct
    context = resource.get("context") or {}
    if isinstance(context, dict):
        enc_list = context.get("encounter") or []
        if isinstance(enc_list, list):
            for entry in enc_list:
                if isinstance(entry, dict):
                    candidate = _extract_ref_id(entry.get("reference"), "Encounter")
                    if candidate:
                        return candidate
    return None


def _is_condition_entered_in_error(resource: dict[str, Any]) -> bool:
    ver_status = resource.get("verificationStatus") or {}
    if not isinstance(ver_status, dict):
        return False
    if str(ver_status.get("text") or "").strip().lower() == "entered-in-error":
        return True
    for coding in ver_status.get("coding") or []:
        if isinstance(coding, dict) and str(coding.get("code") or "").strip().lower() == "entered-in-error":
            return True
    return False


def _contains_forbidden_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _FORBIDDEN_CONTENT_MARKERS)


def _summarize_resource(resource: dict[str, Any], encounter_specialty: str | None = None) -> str:
    rtype = str(resource.get("resourceType") or "")
    if rtype == "Composition":
        title = str(resource.get("title") or "Signed SOAP Note")
        section_divs = [
            str((sec.get("text") or {}).get("div") or "").strip()
            for sec in (resource.get("section") or [])
            if isinstance(sec, dict)
        ]
        extras = [d for d in section_divs if d]
        prefix = f"{encounter_specialty} " if encounter_specialty and encounter_specialty.lower() not in title.lower() else ""
        base = f"{prefix}{title}".strip()
        return f"{base}: {' '.join(extras)}" if extras else base

    if rtype == "Condition":
        code_text = str((resource.get("code") or {}).get("text") or "Condition")
        return code_text

    if rtype in ("MedicationRequest", "MedicationStatement"):
        med_name = str((resource.get("medicationCodeableConcept") or {}).get("text") or "Medication")
        status = str(resource.get("status") or "")
        dosages = resource.get("dosageInstruction") or resource.get("dosage") or []
        dosage_text = (
            str(dosages[0].get("text"))
            if dosages and isinstance(dosages[0], dict) and dosages[0].get("text")
            else ""
        )
        return f"{med_name} ({status})" + (f" - {dosage_text}" if dosage_text else "")

    if rtype == "Observation":
        val = str(resource.get("valueString") or (resource.get("code") or {}).get("text") or "Observation")
        return val

    if rtype in ("DiagnosticReport", "DocumentReference"):
        code_text = str((resource.get("code") or {}).get("text") or resource.get("description") or rtype)
        conclusion = str(resource.get("conclusion") or "").strip()
        return f"{code_text}: {conclusion}" if conclusion else code_text

    if rtype == "Provenance":
        recorded = str(resource.get("recorded") or "")
        return f"Signed provenance attestation ({recorded})" if recorded else "Signed provenance attestation"

    return f"{rtype}/{resource.get('id', '')}"


class InsurerReleaseService:
    """Assembles allowlisted, signed-FHIR-only insurer packets under active ReleaseGrants."""

    def __init__(
        self,
        fhir_store: FhirR4Store,
        grant_service: GrantService | None = None,
    ) -> None:
        self._fhir_store = fhir_store
        self._grant_service = grant_service or GrantService()

    def assemble_allowlisted_packet(
        self,
        grant: ReleaseGrant,
        consent: ConsentRecord,
        checklist_requirements: Sequence[str | ChecklistRequirement],
        now: datetime,
        packet_id: str | None = None,
        packet_version: int = 1,
    ) -> InsurerPacketPayload:
        """Verify grant & consent, read ONLY signed allowlisted FHIR R4 resources, and compute checklist + SHA-256."""
        self._grant_service.verify_grant_active(
            grant=grant,
            consent_record=consent,
            now=now,
        )

        effective_categories = grant.allowed_categories & consent.allowed_categories
        resources = self._fhir_store.list_resources(grant.tenant_id)

        # Map patient's encounters -> (date, specialty)
        patient_encounters: dict[str, tuple[date | None, str | None]] = {}
        for res in resources:
            if res.get("resourceType") != "Encounter":
                continue
            subject_pid = _extract_ref_id((res.get("subject") or {}).get("reference"), "Patient")
            if subject_pid != grant.patient_id:
                continue
            enc_date = _parse_date((res.get("period") or {}).get("start"))
            specialty = (res.get("serviceType") or {}).get("text")
            patient_encounters[str(res["id"])] = (enc_date, str(specialty) if specialty else None)

        # Identify signed (status == "final") Compositions and their referenced clinical resources
        signed_encounter_ids: set[str] = set()
        signed_comp_base_refs: set[str] = set()
        for res in resources:
            if res.get("resourceType") != "Composition":
                continue
            if str(res.get("status") or "").lower() != "final":
                continue
            enc_id = _extract_ref_id((res.get("encounter") or {}).get("reference"), "Encounter")
            subject_pid = _extract_ref_id((res.get("subject") or {}).get("reference"), "Patient")
            if subject_pid == grant.patient_id or (enc_id and enc_id in patient_encounters):
                if enc_id:
                    signed_encounter_ids.add(enc_id)
                signed_comp_base_refs.add(f"Composition/{res['id']}")

        allowlisted_items: list[AllowlistedPacketItem] = []
        for res in resources:
            rtype = str(res.get("resourceType") or "")
            if rtype not in ALLOWLISTED_FHIR_RESOURCE_TYPES:
                continue

            status = str(res.get("status") or "").lower()
            if status in _FORBIDDEN_STATUSES:
                continue
            if rtype in _REQUIRE_FINAL_STATUS_TYPES and status != "final":
                continue
            if rtype == "DocumentReference":
                doc_status = str(res.get("docStatus") or "").lower()
                if status != "current" or doc_status in _FORBIDDEN_STATUSES:
                    continue
            if rtype == "Condition" and _is_condition_entered_in_error(res):
                continue

            category = _RESOURCE_TYPE_TO_CATEGORY[rtype]
            if category not in effective_categories:
                continue

            # Verify patient ownership (directly via subject, via encounter, or via Provenance target)
            enc_id = (
                _extract_document_reference_encounter_id(res)
                if rtype == "DocumentReference"
                else _extract_ref_id((res.get("encounter") or {}).get("reference"), "Encounter")
            )
            subject_pid = _extract_ref_id((res.get("subject") or {}).get("reference"), "Patient")

            if rtype == "Provenance":
                targets = res.get("target") or []
                target_bases = {
                    str(t.get("reference", "")).split("/_history/", 1)[0]
                    for t in targets
                    if isinstance(t, dict)
                }
                if not (target_bases & signed_comp_base_refs):
                    continue
                enc_date = None
                enc_specialty = None
            else:
                belongs_to_patient = (
                    subject_pid == grant.patient_id
                    or (enc_id is not None and enc_id in patient_encounters)
                )
                if not belongs_to_patient:
                    continue
                # Signed-encounter-bound types must belong to a signed (status="final") encounter
                if rtype in ("Condition", "MedicationRequest", "MedicationStatement", "DocumentReference"):
                    if enc_id is None or enc_id not in signed_encounter_ids:
                        continue
                enc_info = patient_encounters.get(enc_id or "")
                enc_date = (
                    enc_info[0]
                    if enc_info and enc_info[0] is not None
                    else _parse_date(
                        res.get("authoredOn")
                        or res.get("effectiveDateTime")
                        or res.get("date")
                    )
                )
                enc_specialty = enc_info[1] if enc_info else None

            summary = _summarize_resource(res, encounter_specialty=enc_specialty)
            if _contains_forbidden_marker(summary):
                continue

            version_id = str((res.get("meta") or {}).get("versionId", "1"))
            allowlisted_items.append(
                AllowlistedPacketItem(
                    resource_type=rtype,
                    resource_id=str(res["id"]),
                    version_id=version_id,
                    category=category,
                    encounter_date=enc_date,
                    summary=summary,
                )
            )

        allowlisted_items.sort(
            key=lambda item: (
                item.encounter_date or date.min,
                item.resource_type,
                item.resource_id,
                item.version_id,
            )
        )

        checklist = self._compute_coverage_checklist(checklist_requirements, allowlisted_items)

        unsigned_payload = InsurerPacketPayload(
            packet_id=packet_id or f"PKT-{uuid.uuid4().hex[:12].upper()}",
            packet_version=packet_version,
            grant_id=grant.grant_id,
            patient_id=grant.patient_id,
            consent_version_id=consent.consent_version_id,
            purpose="prior_auth_documentation",
            status="AWAITING_HUMAN_RELEASE",
            allowlisted_items=allowlisted_items,
            coverage_checklist=checklist,
            packet_sha256=None,
        )
        digest = compute_canonical_sha256(unsigned_payload)
        return unsigned_payload.model_copy(update={"packet_sha256": digest})

    @staticmethod
    def _compute_coverage_checklist(
        requirements: Sequence[str | ChecklistRequirement],
        items: Sequence[AllowlistedPacketItem],
    ) -> CoverageChecklist:
        item_token_sets: list[tuple[AllowlistedPacketItem, set[str]]] = []
        for item in items:
            tokens = (
                _tokenize(item.summary)
                | _tokenize(item.category)
                | _RESOURCE_TYPE_SYNONYMS.get(item.resource_type, frozenset())
            )
            item_token_sets.append((item, tokens))

        satisfied = 0
        missing: list[str] = []

        for req in requirements:
            if isinstance(req, ChecklistRequirement):
                label = req.label
                req_tokens = _tokenize(" ".join(req.keywords)) if req.keywords else _tokenize(label)
                allowed_types = set(req.required_resource_types)
                matched = any(
                    (not allowed_types or item.resource_type in allowed_types)
                    and req_tokens.issubset(tokens)
                    for item, tokens in item_token_sets
                )
            else:
                label = req
                req_tokens = _tokenize(label)
                matched = bool(req_tokens) and any(
                    req_tokens.issubset(tokens) for _, tokens in item_token_sets
                )

            if matched:
                satisfied += 1
            else:
                missing.append(label)

        return CoverageChecklist(
            numerator=satisfied,
            denominator=len(requirements),
            missing_items=missing,
        )


__all__ = [
    "ALLOWLISTED_FHIR_RESOURCE_TYPES",
    "ChecklistRequirement",
    "InsurerReleaseService",
]
