"""Derived tenant-scoped Spanner Graph index for bounded longitudinal continuity retrieval."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, Protocol
import uuid

from pydantic import Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel
from carethread.schemas.v1.continuity import FhirVersionRef
from carethread.schemas.v1.soap import MedicationStatus
from carethread.stores.fhir import FhirR4Store

MAX_ENCOUNTERS_LIMIT: int = 25
MAX_HOPS_LIMIT: int = 2
DEFAULT_GRAPH_TIMEOUT_MS: int = 1500


class SpannerGraphTimeoutError(TimeoutError):
    """Raised when a Spanner Graph continuity query exceeds the latency budget."""


class SpannerGraphUnavailableError(RuntimeError):
    """Raised when the derived Spanner Graph index is unavailable or returns an operational error."""


class PatientNode(StrictModel):
    """Tenant-scoped Patient vertex in the derived Spanner Graph."""

    tenant_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    fhir_version_id: str | None = None
    is_current: bool = True


class OrganizationNode(StrictModel):
    """Tenant-scoped Organization vertex in the derived Spanner Graph."""

    tenant_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    fhir_version_id: str | None = None
    is_current: bool = True


class PractitionerNode(StrictModel):
    """Tenant-scoped Practitioner vertex in the derived Spanner Graph."""

    tenant_id: str = Field(min_length=1)
    practitioner_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    fhir_version_id: str | None = None
    is_current: bool = True


class SpecialtyNode(StrictModel):
    """Tenant-scoped Specialty vertex in the derived Spanner Graph."""

    tenant_id: str = Field(min_length=1)
    specialty_code: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    is_current: bool = True


class PractitionerRoleNode(StrictModel):
    """Tenant-scoped PractitionerRole vertex linking Practitioner, Specialty, and Organization."""

    tenant_id: str = Field(min_length=1)
    role_id: str = Field(min_length=1)
    practitioner_id: str = Field(min_length=1)
    specialty_code: str = Field(min_length=1)
    organization_id: str | None = None
    fhir_version_id: str | None = None
    is_current: bool = True


class EncounterNode(StrictModel):
    """Tenant-scoped Encounter vertex linked to its signed Composition version."""

    tenant_id: str = Field(min_length=1)
    encounter_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    encounter_date: date
    specialty: str = Field(min_length=1)
    practitioner_id: str | None = None
    organization_id: str | None = None
    composition_id: str | None = None
    composition_version_id: str | None = None
    composition_title: str | None = None
    fhir_version_id: str | None = None
    is_current: bool = True


class MedicationConceptNode(StrictModel):
    """Tenant-scoped MedicationConcept vertex."""

    tenant_id: str = Field(min_length=1)
    concept_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    is_current: bool = True


class MedicationEventNode(StrictModel):
    """Tenant-scoped MedicationEvent vertex with explicit clinical status semantics."""

    tenant_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    encounter_id: str = Field(min_length=1)
    encounter_date: date
    medication_concept_id: str = Field(min_length=1)
    medication_name: str = Field(min_length=1)
    medication_status: MedicationStatus
    dosage_text: str | None = None
    summary: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    fhir_resource_type: str = Field(min_length=1)
    fhir_resource_id: str = Field(min_length=1)
    fhir_version_id: str = Field(min_length=1)
    is_current: bool = True


class ProblemEventNode(StrictModel):
    """Tenant-scoped ProblemEvent vertex derived from signed FHIR Condition resources."""

    tenant_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    encounter_id: str = Field(min_length=1)
    encounter_date: date
    statement: str = Field(min_length=1)
    polarity: Literal["PRESENT", "NEGATED", "UNCERTAIN"] = "PRESENT"
    certainty: str | None = None
    clinical_status: str = "active"
    verification_status: str = "confirmed"
    evidence_ids: list[str] = Field(min_length=1)
    fhir_resource_type: str = "Condition"
    fhir_resource_id: str = Field(min_length=1)
    fhir_version_id: str = Field(min_length=1)
    is_current: bool = True


class GraphContinuitySnapshot(StrictModel):
    """Bounded subgraph snapshot returned by SpannerGraphStore.query_patient_continuity."""

    tenant_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    watermark_ts: datetime | None = None
    encounters: list[EncounterNode] = Field(default_factory=list)
    medication_events: list[MedicationEventNode] = Field(default_factory=list)
    problem_events: list[ProblemEventNode] = Field(default_factory=list)
    practitioners: dict[str, PractitionerNode] = Field(default_factory=dict)
    specialties: dict[str, SpecialtyNode] = Field(default_factory=dict)
    organizations: dict[str, OrganizationNode] = Field(default_factory=dict)
    fhir_version_refs: list[FhirVersionRef] = Field(default_factory=list)


class GraphRebuildStats(StrictModel):
    """Summary counts from a full tenant graph rebuild from authoritative FHIR R4."""

    tenant_id: str = Field(min_length=1)
    patients: int = Field(ge=0)
    encounters: int = Field(ge=0)
    practitioners: int = Field(ge=0)
    practitioner_roles: int = Field(ge=0)
    specialties: int = Field(ge=0)
    organizations: int = Field(ge=0)
    medication_concepts: int = Field(ge=0)
    medication_events: int = Field(ge=0)
    problem_events: int = Field(ge=0)


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


def _parse_fhir_date(raw: str | None) -> date:
    if raw and isinstance(raw, str):
        token = raw.strip()[:10]
        try:
            return date.fromisoformat(token)
        except ValueError:
            pass
    return date(1970, 1, 1)


def _parse_note_metadata(resource: dict[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for note in resource.get("note") or []:
        if not isinstance(note, dict):
            continue
        text = note.get("text")
        if not isinstance(text, str):
            continue
        for segment in text.split(";"):
            part = segment.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                metadata[k.strip()] = v.strip()
    return metadata


def _parse_evidence_ids(resource: dict[str, Any], fallback_id: str) -> list[str]:
    meta = _parse_note_metadata(resource)
    raw_ids = meta.get("evidence_ids", "")
    ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
    return ids if ids else [fallback_id]


def _derive_medication_status(resource: dict[str, Any]) -> MedicationStatus:
    meta = _parse_note_metadata(resource)
    explicit = meta.get("medication_status")
    if explicit:
        try:
            return MedicationStatus(explicit)
        except ValueError:
            pass
    if resource.get("resourceType") == "MedicationRequest":
        return MedicationStatus.PRESCRIBED
    status = str(resource.get("status") or "").lower()
    mapping = {
        "not-taken": MedicationStatus.DISCUSSED,
        "intended": MedicationStatus.PLANNED,
        "stopped": MedicationStatus.STOPPED,
        "unknown": MedicationStatus.UNCLEAR,
        "active": MedicationStatus.REPORTED,
    }
    return mapping.get(status, MedicationStatus.UNCLEAR)


def _derive_condition_polarity(resource: dict[str, Any]) -> tuple[Literal["PRESENT", "NEGATED", "UNCERTAIN"], str, str]:
    ver_codings = ((resource.get("verificationStatus") or {}).get("coding") or [])
    clin_codings = ((resource.get("clinicalStatus") or {}).get("coding") or [])
    ver_code = str(ver_codings[0].get("code", "confirmed")) if ver_codings else "confirmed"
    clin_code = str(clin_codings[0].get("code", "active")) if clin_codings else "active"
    if ver_code == "refuted":
        return "NEGATED", clin_code, ver_code
    if ver_code in {"provisional", "unconfirmed", "differential"}:
        return "UNCERTAIN", clin_code, ver_code
    return "PRESENT", clin_code, ver_code


def _extract_practitioner_name(resource: dict[str, Any]) -> str:
    names = resource.get("name") or []
    if isinstance(names, list) and names:
        first = names[0]
        if isinstance(first, dict):
            if first.get("text"):
                return str(first["text"])
            parts = [*(first.get("prefix") or []), *(first.get("given") or [])]
            if first.get("family"):
                parts.append(str(first["family"]))
            joined = " ".join(str(p) for p in parts if p)
            if joined:
                return joined
    return str(resource.get("id", "Unknown Practitioner"))


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "unknown"


class SpannerGraphStore(Protocol):
    """Protocol for the strictly derived, tenant-scoped Spanner Graph continuity index."""

    def upsert_patient(self, node: PatientNode) -> None: ...
    def upsert_organization(self, node: OrganizationNode) -> None: ...
    def upsert_practitioner(self, node: PractitionerNode) -> None: ...
    def upsert_specialty(self, node: SpecialtyNode) -> None: ...
    def upsert_practitioner_role(self, node: PractitionerRoleNode) -> None: ...
    def upsert_encounter(self, node: EncounterNode) -> None: ...
    def upsert_medication_concept(self, node: MedicationConceptNode) -> None: ...
    def upsert_medication_event(self, node: MedicationEventNode) -> None: ...
    def upsert_problem_event(self, node: ProblemEventNode) -> None: ...

    def mark_resource_amended(
        self,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        superseded_version_id: str | None = None,
    ) -> int: ...

    def rebuild_from_fhir(self, tenant_id: str, fhir_store: FhirR4Store) -> GraphRebuildStats: ...

    def query_patient_continuity(
        self,
        tenant_id: str,
        patient_id: str,
        max_encounters: int = 10,
        max_hops: int = MAX_HOPS_LIMIT,
        as_of_date: date | None = None,
    ) -> GraphContinuitySnapshot: ...


class InMemorySpannerGraphStore:
    """In-memory tenant-scoped Spanner Graph index with idempotent upserts, rebuild, and fault injection."""

    def __init__(
        self,
        *,
        simulated_latency_ms: int = 0,
        timeout_ms: int = DEFAULT_GRAPH_TIMEOUT_MS,
        simulate_error: Exception | None = None,
    ) -> None:
        self.simulated_latency_ms = simulated_latency_ms
        self.timeout_ms = timeout_ms
        self.simulate_error = simulate_error

        # All tables strictly use tenant_id as the leading key component.
        self._patients: dict[tuple[str, str], PatientNode] = {}
        self._organizations: dict[tuple[str, str], OrganizationNode] = {}
        self._practitioners: dict[tuple[str, str], PractitionerNode] = {}
        self._specialties: dict[tuple[str, str], SpecialtyNode] = {}
        self._practitioner_roles: dict[tuple[str, str], PractitionerRoleNode] = {}
        self._encounters: dict[tuple[str, str, str], EncounterNode] = {}
        self._medication_concepts: dict[tuple[str, str], MedicationConceptNode] = {}
        self._medication_events: dict[tuple[str, str, str], MedicationEventNode] = {}
        self._problem_events: dict[tuple[str, str, str], ProblemEventNode] = {}
        self._watermarks: dict[str, datetime] = {}

    def _touch_watermark(self, tenant_id: str) -> None:
        self._watermarks[tenant_id] = datetime.now(timezone.utc)

    def upsert_patient(self, node: PatientNode) -> None:
        self._patients[(node.tenant_id, node.patient_id)] = node
        self._touch_watermark(node.tenant_id)

    def upsert_organization(self, node: OrganizationNode) -> None:
        self._organizations[(node.tenant_id, node.organization_id)] = node
        self._touch_watermark(node.tenant_id)

    def upsert_practitioner(self, node: PractitionerNode) -> None:
        self._practitioners[(node.tenant_id, node.practitioner_id)] = node
        self._touch_watermark(node.tenant_id)

    def upsert_specialty(self, node: SpecialtyNode) -> None:
        self._specialties[(node.tenant_id, node.specialty_code)] = node
        self._touch_watermark(node.tenant_id)

    def upsert_practitioner_role(self, node: PractitionerRoleNode) -> None:
        self._practitioner_roles[(node.tenant_id, node.role_id)] = node
        self._touch_watermark(node.tenant_id)

    def upsert_encounter(self, node: EncounterNode) -> None:
        version_key = node.composition_version_id or node.fhir_version_id or "1"
        if node.is_current:
            for (tid, eid, vkey), existing in list(self._encounters.items()):
                if tid == node.tenant_id and eid == node.encounter_id and vkey != version_key and existing.is_current:
                    self._encounters[(tid, eid, vkey)] = existing.model_copy(update={"is_current": False})
        self._encounters[(node.tenant_id, node.encounter_id, version_key)] = node
        self._touch_watermark(node.tenant_id)

    def upsert_medication_concept(self, node: MedicationConceptNode) -> None:
        self._medication_concepts[(node.tenant_id, node.concept_id)] = node
        self._touch_watermark(node.tenant_id)

    def upsert_medication_event(self, node: MedicationEventNode) -> None:
        if node.is_current:
            for (tid, rid, vid), existing in list(self._medication_events.items()):
                if (
                    tid == node.tenant_id
                    and rid == node.fhir_resource_id
                    and vid != node.fhir_version_id
                    and existing.is_current
                ):
                    self._medication_events[(tid, rid, vid)] = existing.model_copy(
                        update={"is_current": False}
                    )
        self._medication_events[(node.tenant_id, node.fhir_resource_id, node.fhir_version_id)] = node
        self._touch_watermark(node.tenant_id)

    def upsert_problem_event(self, node: ProblemEventNode) -> None:
        if node.is_current:
            for (tid, rid, vid), existing in list(self._problem_events.items()):
                if (
                    tid == node.tenant_id
                    and rid == node.fhir_resource_id
                    and vid != node.fhir_version_id
                    and existing.is_current
                ):
                    self._problem_events[(tid, rid, vid)] = existing.model_copy(
                        update={"is_current": False}
                    )
        self._problem_events[(node.tenant_id, node.fhir_resource_id, node.fhir_version_id)] = node
        self._touch_watermark(node.tenant_id)

    def mark_resource_amended(
        self,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        superseded_version_id: str | None = None,
    ) -> int:
        """Mark projected rows for an amended FHIR resource as is_current=False."""
        updated_count = 0
        if resource_type in ("Composition", "Encounter"):
            for key, enc in list(self._encounters.items()):
                if key[0] != tenant_id or not enc.is_current:
                    continue
                matches_comp = resource_type == "Composition" and enc.composition_id == resource_id
                matches_enc = resource_type == "Encounter" and enc.encounter_id == resource_id
                if not (matches_comp or matches_enc):
                    continue
                current_vid = enc.composition_version_id if matches_comp else enc.fhir_version_id
                if superseded_version_id is not None and current_vid != superseded_version_id:
                    continue
                self._encounters[key] = enc.model_copy(update={"is_current": False})
                updated_count += 1
        elif resource_type in ("MedicationRequest", "MedicationStatement"):
            for key, med in list(self._medication_events.items()):
                if key[0] != tenant_id or not med.is_current:
                    continue
                if med.fhir_resource_type == resource_type and med.fhir_resource_id == resource_id:
                    if superseded_version_id is None or med.fhir_version_id == superseded_version_id:
                        self._medication_events[key] = med.model_copy(update={"is_current": False})
                        updated_count += 1
        elif resource_type == "Condition":
            for key, prob in list(self._problem_events.items()):
                if key[0] != tenant_id or not prob.is_current:
                    continue
                if prob.fhir_resource_id == resource_id:
                    if superseded_version_id is None or prob.fhir_version_id == superseded_version_id:
                        self._problem_events[key] = prob.model_copy(update={"is_current": False})
                        updated_count += 1
        if updated_count > 0:
            self._touch_watermark(tenant_id)
        return updated_count

    def _clear_tenant(self, tenant_id: str) -> None:
        self._patients = {k: v for k, v in self._patients.items() if k[0] != tenant_id}
        self._organizations = {k: v for k, v in self._organizations.items() if k[0] != tenant_id}
        self._practitioners = {k: v for k, v in self._practitioners.items() if k[0] != tenant_id}
        self._specialties = {k: v for k, v in self._specialties.items() if k[0] != tenant_id}
        self._practitioner_roles = {k: v for k, v in self._practitioner_roles.items() if k[0] != tenant_id}
        self._encounters = {k: v for k, v in self._encounters.items() if k[0] != tenant_id}
        self._medication_concepts = {k: v for k, v in self._medication_concepts.items() if k[0] != tenant_id}
        self._medication_events = {k: v for k, v in self._medication_events.items() if k[0] != tenant_id}
        self._problem_events = {k: v for k, v in self._problem_events.items() if k[0] != tenant_id}

    def rebuild_from_fhir(self, tenant_id: str, fhir_store: FhirR4Store) -> GraphRebuildStats:
        """Deterministically rebuild a tenant's entire Spanner Graph projection from FHIR R4."""
        self._clear_tenant(tenant_id)
        resources = fhir_store.list_resources(tenant_id)

        by_type: dict[str, list[dict[str, Any]]] = {}
        for res in resources:
            rtype = str(res.get("resourceType") or "")
            by_type.setdefault(rtype, []).append(res)

        for pat in by_type.get("Patient", []):
            self.upsert_patient(
                PatientNode(
                    tenant_id=tenant_id,
                    patient_id=str(pat["id"]),
                    fhir_version_id=str((pat.get("meta") or {}).get("versionId", "1")),
                )
            )

        for org in by_type.get("Organization", []):
            self.upsert_organization(
                OrganizationNode(
                    tenant_id=tenant_id,
                    organization_id=str(org["id"]),
                    display_name=str(org.get("name") or org["id"]),
                    fhir_version_id=str((org.get("meta") or {}).get("versionId", "1")),
                )
            )

        for prac in by_type.get("Practitioner", []):
            self.upsert_practitioner(
                PractitionerNode(
                    tenant_id=tenant_id,
                    practitioner_id=str(prac["id"]),
                    display_name=_extract_practitioner_name(prac),
                    fhir_version_id=str((prac.get("meta") or {}).get("versionId", "1")),
                )
            )

        prac_to_specialty: dict[str, str] = {}
        for role in by_type.get("PractitionerRole", []):
            prac_id = _extract_ref_id((role.get("practitioner") or {}).get("reference"), "Practitioner")
            org_id = _extract_ref_id((role.get("organization") or {}).get("reference"), "Organization")
            spec_list = role.get("specialty") or []
            spec_display = "General"
            spec_code = "general"
            if spec_list and isinstance(spec_list[0], dict):
                first_spec = spec_list[0]
                codings = first_spec.get("coding") or []
                if codings and isinstance(codings[0], dict):
                    spec_code = str(codings[0].get("code") or _slugify(str(first_spec.get("text") or "general")))
                    spec_display = str(codings[0].get("display") or first_spec.get("text") or spec_code)
                elif first_spec.get("text"):
                    spec_display = str(first_spec["text"])
                    spec_code = _slugify(spec_display)
            self.upsert_specialty(
                SpecialtyNode(
                    tenant_id=tenant_id,
                    specialty_code=spec_code,
                    display_name=spec_display,
                )
            )
            if prac_id:
                prac_to_specialty[prac_id] = spec_display
                self.upsert_practitioner_role(
                    PractitionerRoleNode(
                        tenant_id=tenant_id,
                        role_id=str(role["id"]),
                        practitioner_id=prac_id,
                        specialty_code=spec_code,
                        organization_id=org_id,
                        fhir_version_id=str((role.get("meta") or {}).get("versionId", "1")),
                    )
                )

        # Index signed (status == "final") Compositions by encounter_id
        final_comp_by_encounter: dict[str, dict[str, Any]] = {}
        for comp in by_type.get("Composition", []):
            if str(comp.get("status") or "").lower() != "final":
                continue
            enc_id = _extract_ref_id((comp.get("encounter") or {}).get("reference"), "Encounter")
            if enc_id:
                final_comp_by_encounter[enc_id] = comp

        encounter_meta: dict[str, EncounterNode] = {}
        for enc in by_type.get("Encounter", []):
            enc_id = str(enc["id"])
            patient_id = _extract_ref_id((enc.get("subject") or {}).get("reference"), "Patient")
            if not patient_id:
                continue
            enc_date = _parse_fhir_date((enc.get("period") or {}).get("start"))
            prac_id = None
            for part in enc.get("participant") or []:
                if isinstance(part, dict):
                    candidate = _extract_ref_id((part.get("individual") or {}).get("reference"), "Practitioner")
                    if candidate:
                        prac_id = candidate
                        break
            org_id = _extract_ref_id((enc.get("serviceProvider") or {}).get("reference"), "Organization")
            specialty_text = (enc.get("serviceType") or {}).get("text")
            if not specialty_text:
                enc_types = enc.get("type") or []
                if enc_types and isinstance(enc_types[0], dict):
                    specialty_text = enc_types[0].get("text")
            specialty_display = str(specialty_text or (prac_to_specialty.get(prac_id or "") if prac_id else None) or "General")
            spec_code = _slugify(specialty_display)
            self.upsert_specialty(
                SpecialtyNode(
                    tenant_id=tenant_id,
                    specialty_code=spec_code,
                    display_name=specialty_display,
                )
            )
            comp = final_comp_by_encounter.get(enc_id)
            node = EncounterNode(
                tenant_id=tenant_id,
                encounter_id=enc_id,
                patient_id=patient_id,
                encounter_date=enc_date,
                specialty=specialty_display,
                practitioner_id=prac_id,
                organization_id=org_id,
                composition_id=str(comp["id"]) if comp else None,
                composition_version_id=str((comp.get("meta") or {}).get("versionId", "1")) if comp else None,
                composition_title=str(comp.get("title") or "Signed SOAP Note") if comp else None,
                fhir_version_id=str((enc.get("meta") or {}).get("versionId", "1")),
            )
            self.upsert_encounter(node)
            encounter_meta[enc_id] = node

        for rtype in ("MedicationRequest", "MedicationStatement"):
            for med in by_type.get(rtype, []):
                enc_id = _extract_ref_id((med.get("encounter") or {}).get("reference"), "Encounter")
                enc_node = encounter_meta.get(enc_id or "")
                patient_id = (
                    _extract_ref_id((med.get("subject") or {}).get("reference"), "Patient")
                    or (enc_node.patient_id if enc_node else None)
                )
                if not patient_id or not enc_id:
                    continue
                med_name = str((med.get("medicationCodeableConcept") or {}).get("text") or "Unnamed Medication")
                concept_id = f"MED-{_slugify(med_name)}"
                self.upsert_medication_concept(
                    MedicationConceptNode(
                        tenant_id=tenant_id,
                        concept_id=concept_id,
                        name=med_name,
                    )
                )
                med_status = _derive_medication_status(med)
                raw_date = med.get("authoredOn") or med.get("effectiveDateTime")
                event_date = _parse_fhir_date(raw_date) if raw_date else (enc_node.encounter_date if enc_node else date(1970, 1, 1))
                dosage_list = med.get("dosageInstruction") or med.get("dosage") or []
                dosage_text = (
                    str(dosage_list[0].get("text"))
                    if dosage_list and isinstance(dosage_list[0], dict) and dosage_list[0].get("text")
                    else None
                )
                summary = f"{med_name} ({med_status.value})" + (f" - {dosage_text}" if dosage_text else "")
                version_id = str((med.get("meta") or {}).get("versionId", "1"))
                self.upsert_medication_event(
                    MedicationEventNode(
                        tenant_id=tenant_id,
                        event_id=f"{rtype}:{med['id']}:v{version_id}",
                        patient_id=patient_id,
                        encounter_id=enc_id,
                        encounter_date=event_date,
                        medication_concept_id=concept_id,
                        medication_name=med_name,
                        medication_status=med_status,
                        dosage_text=dosage_text,
                        summary=summary,
                        evidence_ids=_parse_evidence_ids(med, f"{rtype}/{med['id']}"),
                        fhir_resource_type=rtype,
                        fhir_resource_id=str(med["id"]),
                        fhir_version_id=version_id,
                    )
                )

        for cond in by_type.get("Condition", []):
            enc_id = _extract_ref_id((cond.get("encounter") or {}).get("reference"), "Encounter")
            enc_node = encounter_meta.get(enc_id or "")
            patient_id = (
                _extract_ref_id((cond.get("subject") or {}).get("reference"), "Patient")
                or (enc_node.patient_id if enc_node else None)
            )
            if not patient_id or not enc_id:
                continue
            statement = str((cond.get("code") or {}).get("text") or "Unspecified Condition")
            polarity, clin_status, ver_status = _derive_condition_polarity(cond)
            event_date = enc_node.encounter_date if enc_node else _parse_fhir_date(cond.get("recordedDate"))
            version_id = str((cond.get("meta") or {}).get("versionId", "1"))
            self.upsert_problem_event(
                ProblemEventNode(
                    tenant_id=tenant_id,
                    event_id=f"Condition:{cond['id']}:v{version_id}",
                    patient_id=patient_id,
                    encounter_id=enc_id,
                    encounter_date=event_date,
                    statement=statement,
                    polarity=polarity,
                    certainty=ver_status,
                    clinical_status=clin_status,
                    verification_status=ver_status,
                    evidence_ids=_parse_evidence_ids(cond, f"Condition/{cond['id']}"),
                    fhir_resource_type="Condition",
                    fhir_resource_id=str(cond["id"]),
                    fhir_version_id=version_id,
                )
            )

        self._touch_watermark(tenant_id)
        return GraphRebuildStats(
            tenant_id=tenant_id,
            patients=sum(1 for k in self._patients if k[0] == tenant_id),
            encounters=sum(1 for k, v in self._encounters.items() if k[0] == tenant_id and v.is_current),
            practitioners=sum(1 for k in self._practitioners if k[0] == tenant_id),
            practitioner_roles=sum(1 for k in self._practitioner_roles if k[0] == tenant_id),
            specialties=sum(1 for k in self._specialties if k[0] == tenant_id),
            organizations=sum(1 for k in self._organizations if k[0] == tenant_id),
            medication_concepts=sum(1 for k in self._medication_concepts if k[0] == tenant_id),
            medication_events=sum(1 for k, v in self._medication_events.items() if k[0] == tenant_id and v.is_current),
            problem_events=sum(1 for k, v in self._problem_events.items() if k[0] == tenant_id and v.is_current),
        )

    def query_patient_continuity(
        self,
        tenant_id: str,
        patient_id: str,
        max_encounters: int = 10,
        max_hops: int = MAX_HOPS_LIMIT,
        as_of_date: date | None = None,
    ) -> GraphContinuitySnapshot:
        """Execute a strictly bounded, tenant-scoped continuity subgraph query (never evaluates authz)."""
        if max_encounters < 1 or max_encounters > MAX_ENCOUNTERS_LIMIT:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="GRAPH_MAX_ENCOUNTERS_OUT_OF_BOUNDS",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="max_encounters",
                    guided_recovery=f"Set max_encounters between 1 and {MAX_ENCOUNTERS_LIMIT}.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        if max_hops < 1 or max_hops > MAX_HOPS_LIMIT:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="GRAPH_MAX_HOPS_OUT_OF_BOUNDS",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="max_hops",
                    guided_recovery=f"Set max_hops between 1 and {MAX_HOPS_LIMIT}.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        if self.simulate_error is not None:
            raise self.simulate_error
        if self.simulated_latency_ms > self.timeout_ms:
            raise SpannerGraphTimeoutError(
                f"Spanner Graph query exceeded timeout budget ({self.simulated_latency_ms}ms > {self.timeout_ms}ms)"
            )

        # Hop 1: Patient -> Encounter (strictly tenant-prefixed and is_current=True)
        encounters = [
            enc
            for (tid, _, _), enc in self._encounters.items()
            if tid == tenant_id
            and enc.patient_id == patient_id
            and enc.is_current
            and (as_of_date is None or enc.encounter_date <= as_of_date)
        ]
        encounters.sort(key=lambda e: (e.encounter_date, e.encounter_id), reverse=True)
        bounded_encounters = encounters[:max_encounters]
        encounter_ids = {enc.encounter_id for enc in bounded_encounters}

        med_events: list[MedicationEventNode] = []
        problem_events: list[ProblemEventNode] = []
        practitioners: dict[str, PractitionerNode] = {}
        specialties: dict[str, SpecialtyNode] = {}
        organizations: dict[str, OrganizationNode] = {}

        if max_hops >= 2 and encounter_ids:
            # Hop 2: Encounter -> MedicationEvent, ProblemEvent, Practitioner, Specialty, Organization
            med_events = [
                med
                for (tid, _, _), med in self._medication_events.items()
                if tid == tenant_id
                and med.patient_id == patient_id
                and med.encounter_id in encounter_ids
                and med.is_current
            ]
            med_events.sort(key=lambda m: (m.encounter_date, m.fhir_resource_id))

            problem_events = [
                prob
                for (tid, _, _), prob in self._problem_events.items()
                if tid == tenant_id
                and prob.patient_id == patient_id
                and prob.encounter_id in encounter_ids
                and prob.is_current
            ]
            problem_events.sort(key=lambda p: (p.encounter_date, p.fhir_resource_id))

            for enc in bounded_encounters:
                if enc.practitioner_id:
                    prac = self._practitioners.get((tenant_id, enc.practitioner_id))
                    if prac is not None:
                        practitioners[prac.practitioner_id] = prac
                if enc.organization_id:
                    org = self._organizations.get((tenant_id, enc.organization_id))
                    if org is not None:
                        organizations[org.organization_id] = org
                spec_code = _slugify(enc.specialty)
                spec = self._specialties.get((tenant_id, spec_code))
                if spec is not None:
                    specialties[spec.specialty_code] = spec

        ref_map: dict[tuple[str, str], FhirVersionRef] = {}
        for enc in bounded_encounters:
            if enc.fhir_version_id:
                ref_map[("Encounter", enc.encounter_id)] = FhirVersionRef(
                    resource_type="Encounter",
                    resource_id=enc.encounter_id,
                    version_id=enc.fhir_version_id,
                )
            if enc.composition_id and enc.composition_version_id:
                ref_map[("Composition", enc.composition_id)] = FhirVersionRef(
                    resource_type="Composition",
                    resource_id=enc.composition_id,
                    version_id=enc.composition_version_id,
                )
        for med in med_events:
            ref_map[(med.fhir_resource_type, med.fhir_resource_id)] = FhirVersionRef(
                resource_type=med.fhir_resource_type,
                resource_id=med.fhir_resource_id,
                version_id=med.fhir_version_id,
            )
        for prob in problem_events:
            ref_map[(prob.fhir_resource_type, prob.fhir_resource_id)] = FhirVersionRef(
                resource_type=prob.fhir_resource_type,
                resource_id=prob.fhir_resource_id,
                version_id=prob.fhir_version_id,
            )

        sorted_refs = sorted(
            ref_map.values(),
            key=lambda r: (r.resource_type, r.resource_id, r.version_id),
        )

        return GraphContinuitySnapshot(
            tenant_id=tenant_id,
            patient_id=patient_id,
            watermark_ts=self._watermarks.get(tenant_id),
            encounters=bounded_encounters,
            medication_events=med_events,
            problem_events=problem_events,
            practitioners=practitioners,
            specialties=specialties,
            organizations=organizations,
            fhir_version_refs=sorted_refs,
        )


__all__ = [
    "DEFAULT_GRAPH_TIMEOUT_MS",
    "MAX_ENCOUNTERS_LIMIT",
    "MAX_HOPS_LIMIT",
    "EncounterNode",
    "GraphContinuitySnapshot",
    "GraphRebuildStats",
    "InMemorySpannerGraphStore",
    "MedicationConceptNode",
    "MedicationEventNode",
    "OrganizationNode",
    "PatientNode",
    "PractitionerNode",
    "PractitionerRoleNode",
    "ProblemEventNode",
    "SpannerGraphStore",
    "SpannerGraphTimeoutError",
    "SpannerGraphUnavailableError",
    "SpecialtyNode",
]
