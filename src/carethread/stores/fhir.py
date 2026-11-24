"""Authoritative FHIR R4 store and deterministic signed-SOAP FHIR writer."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Protocol
import uuid

from pydantic import Field

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel
from carethread.schemas.v1.soap import (
    MedicationItem,
    MedicationStatus,
    SoapDraftPayload,
    SoapItem,
    compute_canonical_sha256,
)

IDEMPOTENCY_SYSTEM = "urn:carethread:idempotency"

# Clinical safety mapping: non-prescribed statuses NEVER become MedicationRequest
# and NEVER become MedicationStatement(status="active").
_NON_PRESCRIBED_STATEMENT_STATUS: dict[MedicationStatus, str] = {
    MedicationStatus.DISCUSSED: "not-taken",
    MedicationStatus.PLANNED: "intended",
    MedicationStatus.REPORTED: "unknown",
    MedicationStatus.STOPPED: "stopped",
    MedicationStatus.UNCLEAR: "unknown",
}


class FhirPreconditionFailedError(GuidedErrorException):
    """Raised when an If-Match optimistic concurrency check fails."""


class FhirResourceNotFoundError(GuidedErrorException):
    """Raised when a requested FHIR resource or version does not exist."""


class FhirCommitResult(StrictModel):
    """Result of committing or reconciling a signed SOAP note in FHIR R4."""

    tenant_id: str = Field(min_length=1)
    encounter_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    composition_ref: str = Field(min_length=1)
    provenance_ref: str = Field(min_length=1)
    resource_refs: list[str] = Field(default_factory=list)
    created_new: bool
    draft_sha256: str | None = None


def _extract_identifiers(resource: dict[str, Any]) -> list[dict[str, Any]]:
    raw = resource.get("identifier")
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _matches_identifier(resource: dict[str, Any], identifier_query: str) -> bool:
    if "|" in identifier_query:
        expected_system, expected_value = identifier_query.split("|", 1)
    else:
        expected_system, expected_value = None, identifier_query

    for ident in _extract_identifiers(resource):
        value = ident.get("value")
        system = ident.get("system")
        if value != expected_value:
            continue
        if expected_system is None or system == expected_system:
            return True
    return False


def _parse_if_none_exist_identifier(if_none_exist: str) -> str:
    query = if_none_exist.strip()
    if query.startswith("identifier="):
        return query[len("identifier=") :]
    return query


def _normalize_etag(if_match: str) -> str:
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    return value.strip('"')


def _versioned_ref(resource: dict[str, Any]) -> str:
    return f"{resource['resourceType']}/{resource['id']}/_history/{resource['meta']['versionId']}"


def _base_ref(ref: str) -> str:
    if "/_history/" in ref:
        return ref.split("/_history/", 1)[0]
    return ref


class FhirR4Store(Protocol):
    """Protocol for an authoritative, versioned, tenant-scoped FHIR R4 store."""

    enable_resource_versioning: bool

    def create(
        self,
        tenant_id: str,
        resource: dict[str, Any],
        if_none_exist: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a resource, supporting If-None-Exist conditional create."""
        ...

    def read(self, tenant_id: str, resource_type: str, resource_id: str) -> dict[str, Any]:
        """Read the latest version of a FHIR resource."""
        ...

    def vread(
        self,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        """Read an exact historical version of a FHIR resource."""
        ...

    def update(
        self,
        tenant_id: str,
        resource: dict[str, Any],
        if_match: str | None = None,
    ) -> dict[str, Any]:
        """Update a resource with optional If-Match optimistic concurrency control."""
        ...

    def search(
        self,
        tenant_id: str,
        resource_type: str,
        identifier: str | None = None,
        encounter: str | None = None,
        revinclude: str | None = None,
    ) -> dict[str, Any]:
        """Search resources and optionally revinclude targeting Provenance records."""
        ...

    def transaction(self, tenant_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
        """Execute a FHIR R4 transaction bundle atomically."""
        ...

    def list_resources(
        self,
        tenant_id: str,
        resource_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List latest versions of resources for a tenant."""
        ...


class InMemoryFhirR4Store:
    """In-memory FHIR R4 store with versioning, conditional create, If-Match, and fault injection."""

    def __init__(
        self,
        enable_resource_versioning: bool = True,
        simulate_lost_response_once: bool = False,
    ) -> None:
        self.enable_resource_versioning = enable_resource_versioning
        self.simulate_lost_response_once = simulate_lost_response_once
        # tenant_id -> (resourceType, id) -> list of historical resource dicts
        self._records: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {}

    def _tenant_bucket(self, tenant_id: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
        return self._records.setdefault(tenant_id, {})

    def _find_by_identifier(
        self,
        tenant_id: str,
        resource_type: str,
        identifier_query: str,
    ) -> dict[str, Any] | None:
        bucket = self._tenant_bucket(tenant_id)
        for (rtype, _), history in bucket.items():
            if rtype != resource_type or not history:
                continue
            latest = history[-1]
            if _matches_identifier(latest, identifier_query):
                return copy.deepcopy(latest)
        return None

    def _create_internal(
        self,
        tenant_id: str,
        resource: dict[str, Any],
        if_none_exist: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        resource_type = resource.get("resourceType")
        if not isinstance(resource_type, str) or not resource_type:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="FHIR_MISSING_RESOURCE_TYPE",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="resourceType",
                    guided_recovery="Provide a valid FHIR R4 resourceType.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        if if_none_exist:
            identifier_query = _parse_if_none_exist_identifier(if_none_exist)
            existing = self._find_by_identifier(tenant_id, resource_type, identifier_query)
            if existing is not None:
                return existing, False

        stored = copy.deepcopy(resource)
        resource_id = stored.get("id") or uuid.uuid4().hex
        stored["id"] = resource_id
        now_iso = datetime.now(timezone.utc).isoformat()
        meta = dict(stored.get("meta") or {})
        meta["versionId"] = "1"
        meta["lastUpdated"] = now_iso
        stored["meta"] = meta

        bucket = self._tenant_bucket(tenant_id)
        bucket[(resource_type, resource_id)] = [copy.deepcopy(stored)]
        return copy.deepcopy(stored), True

    def create(
        self,
        tenant_id: str,
        resource: dict[str, Any],
        if_none_exist: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a FHIR resource with optional If-None-Exist conditional create."""
        stored, created = self._create_internal(tenant_id, resource, if_none_exist=if_none_exist)
        if self.simulate_lost_response_once:
            self.simulate_lost_response_once = False
            raise TimeoutError("Simulated lost response after FHIR create")
        return stored, created

    def read(self, tenant_id: str, resource_type: str, resource_id: str) -> dict[str, Any]:
        """Read the latest version of a FHIR resource."""
        history = self._tenant_bucket(tenant_id).get((resource_type, resource_id))
        if not history:
            raise FhirResourceNotFoundError(
                GuidedErrorEnvelope(
                    error_code="FHIR_RESOURCE_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path=f"{resource_type}/{resource_id}",
                    guided_recovery="Verify the resource ID and tenant scope.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        return copy.deepcopy(history[-1])

    def vread(
        self,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        """Read an exact historical version of a FHIR resource."""
        history = self._tenant_bucket(tenant_id).get((resource_type, resource_id))
        if not history:
            raise FhirResourceNotFoundError(
                GuidedErrorEnvelope(
                    error_code="FHIR_RESOURCE_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path=f"{resource_type}/{resource_id}/_history/{version_id}",
                    guided_recovery="Verify the resource ID and version exist.",
                    trace_id=uuid.uuid4().hex,
                )
            )
        for item in history:
            if item.get("meta", {}).get("versionId") == version_id:
                return copy.deepcopy(item)
        raise FhirResourceNotFoundError(
            GuidedErrorEnvelope(
                error_code="FHIR_VERSION_NOT_FOUND",
                category=ErrorCategory.VALIDATION_RECOVERABLE,
                retryable=False,
                max_retries_remaining=0,
                field_path=f"{resource_type}/{resource_id}/_history/{version_id}",
                guided_recovery="Request an existing FHIR resource versionId.",
                trace_id=uuid.uuid4().hex,
            )
        )

    def update(
        self,
        tenant_id: str,
        resource: dict[str, Any],
        if_match: str | None = None,
    ) -> dict[str, Any]:
        """Update a resource with optional If-Match optimistic locking."""
        resource_type = resource.get("resourceType")
        resource_id = resource.get("id")
        if not isinstance(resource_type, str) or not isinstance(resource_id, str):
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="FHIR_INVALID_UPDATE",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="id",
                    guided_recovery="Provide both resourceType and id when updating a FHIR resource.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        bucket = self._tenant_bucket(tenant_id)
        history = bucket.get((resource_type, resource_id))
        if not history:
            raise FhirResourceNotFoundError(
                GuidedErrorEnvelope(
                    error_code="FHIR_RESOURCE_NOT_FOUND",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path=f"{resource_type}/{resource_id}",
                    guided_recovery="Create the resource before attempting an update.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        current_version = history[-1]["meta"]["versionId"]
        if if_match is not None and _normalize_etag(if_match) != current_version:
            raise FhirPreconditionFailedError(
                GuidedErrorEnvelope(
                    error_code="FHIR_IF_MATCH_PRECONDITION_FAILED",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=True,
                    max_retries_remaining=1,
                    field_path="meta.versionId",
                    guided_recovery="Reload the latest FHIR resource version before retrying update.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        updated = copy.deepcopy(resource)
        next_version = str(int(current_version) + 1)
        meta = dict(updated.get("meta") or {})
        meta["versionId"] = next_version
        meta["lastUpdated"] = datetime.now(timezone.utc).isoformat()
        updated["meta"] = meta

        if self.enable_resource_versioning:
            history.append(copy.deepcopy(updated))
        else:
            history[-1] = copy.deepcopy(updated)
        return copy.deepcopy(updated)

    def search(
        self,
        tenant_id: str,
        resource_type: str,
        identifier: str | None = None,
        encounter: str | None = None,
        revinclude: str | None = None,
    ) -> dict[str, Any]:
        """Search tenant-scoped FHIR resources with optional _revinclude=Provenance:target."""
        bucket = self._tenant_bucket(tenant_id)
        matches: list[dict[str, Any]] = []

        normalized_encounter = None
        if encounter is not None:
            normalized_encounter = (
                encounter if encounter.startswith("Encounter/") else f"Encounter/{encounter}"
            )

        for (rtype, _), history in bucket.items():
            if rtype != resource_type or not history:
                continue
            latest = history[-1]
            if identifier is not None and not _matches_identifier(latest, identifier):
                continue
            if normalized_encounter is not None:
                enc_ref = (latest.get("encounter") or {}).get("reference")
                if enc_ref != normalized_encounter:
                    continue
            matches.append(copy.deepcopy(latest))

        entries: list[dict[str, Any]] = [
            {"resource": resource, "search": {"mode": "match"}} for resource in matches
        ]

        if revinclude in ("Provenance:target", "_revinclude=Provenance:target") and matches:
            matched_base_refs = {f"{m['resourceType']}/{m['id']}" for m in matches}
            matched_versioned_refs = {_versioned_ref(m) for m in matches}
            for (rtype, _), history in bucket.items():
                if rtype != "Provenance" or not history:
                    continue
                prov = history[-1]
                targets = prov.get("target") or []
                target_refs = {
                    t.get("reference", "") for t in targets if isinstance(t, dict)
                }
                if any(
                    ref in matched_versioned_refs or _base_ref(ref) in matched_base_refs
                    for ref in target_refs
                ):
                    entries.append(
                        {"resource": copy.deepcopy(prov), "search": {"mode": "include"}}
                    )

        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(matches),
            "entry": entries,
        }

    def transaction(self, tenant_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
        """Execute a FHIR R4 transaction bundle atomically, honoring If-None-Exist."""
        if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="FHIR_INVALID_TRANSACTION_BUNDLE",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="type",
                    guided_recovery="Submit a FHIR Bundle with type='transaction'.",
                    trace_id=uuid.uuid4().hex,
                )
            )

        response_entries: list[dict[str, Any]] = []
        full_url_to_versioned: dict[str, str] = {}
        full_url_to_relative: dict[str, str] = {}

        def _rewrite_refs(obj: Any) -> Any:
            if isinstance(obj, dict):
                rewritten: dict[str, Any] = {}
                for k, v in obj.items():
                    if k == "reference" and isinstance(v, str):
                        if v.endswith("/_history") and v[: -len("/_history")] in full_url_to_versioned:
                            rewritten[k] = full_url_to_versioned[v[: -len("/_history")]]
                        elif v in full_url_to_versioned:
                            rewritten[k] = full_url_to_versioned[v]
                        elif v in full_url_to_relative:
                            rewritten[k] = full_url_to_relative[v]
                        else:
                            rewritten[k] = v
                    else:
                        rewritten[k] = _rewrite_refs(v)
                return rewritten
            if isinstance(obj, list):
                return [_rewrite_refs(item) for item in obj]
            return obj

        had_tenant = tenant_id in self._records
        tenant_snapshot = copy.deepcopy(self._records[tenant_id]) if had_tenant else None

        try:
            for entry in bundle.get("entry") or []:
                if not isinstance(entry, dict) or not isinstance(entry.get("resource"), dict):
                    raise GuidedErrorException(
                        GuidedErrorEnvelope(
                            error_code="FHIR_INVALID_TRANSACTION_ENTRY",
                            category=ErrorCategory.VALIDATION_RECOVERABLE,
                            retryable=False,
                            max_retries_remaining=0,
                            field_path="entry.resource",
                            guided_recovery="Each FHIR transaction bundle entry must contain a resource object.",
                            trace_id=uuid.uuid4().hex,
                        )
                    )
                resource = _rewrite_refs(entry["resource"])
                request = entry.get("request") or {}
                if_none_exist = request.get("ifNoneExist")
                stored, created = self._create_internal(
                    tenant_id=tenant_id,
                    resource=resource,
                    if_none_exist=if_none_exist,
                )
                full_url = entry.get("fullUrl")
                if isinstance(full_url, str) and full_url:
                    full_url_to_versioned[full_url] = _versioned_ref(stored)
                    full_url_to_relative[full_url] = f"{stored['resourceType']}/{stored['id']}"

                response_entries.append(
                    {
                        "resource": stored,
                        "response": {
                            "status": "201 Created" if created else "200 OK",
                            "location": _versioned_ref(stored),
                            "etag": f'W/"{stored["meta"]["versionId"]}"',
                            "created": created,
                        },
                    }
                )
        except Exception:
            if had_tenant and tenant_snapshot is not None:
                self._records[tenant_id] = tenant_snapshot
            else:
                self._records.pop(tenant_id, None)
            raise

        if self.simulate_lost_response_once:
            self.simulate_lost_response_once = False
            raise TimeoutError("Simulated lost response after FHIR transaction commit")

        return {
            "resourceType": "Bundle",
            "type": "transaction-response",
            "entry": response_entries,
        }

    def list_resources(
        self,
        tenant_id: str,
        resource_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List latest versions of resources stored for a tenant."""
        bucket = self._tenant_bucket(tenant_id)
        results: list[dict[str, Any]] = []
        for (rtype, _), history in bucket.items():
            if resource_type is not None and rtype != resource_type:
                continue
            if history:
                results.append(copy.deepcopy(history[-1]))
        return results


class DeterministicFhirWriter:
    """Deterministic mapper from clinician-signed SOAP drafts to FHIR R4 resources."""

    def __init__(self, store: FhirR4Store) -> None:
        self._store = store

    def _build_observation(
        self,
        encounter_id: str,
        section: str,
        item: SoapItem,
        idempotency_key: str,
    ) -> tuple[str, dict[str, Any], str]:
        ident_val = f"{idempotency_key}:observation:{item.item_id}"
        full_url = f"urn:uuid:obs-{item.item_id}"
        resource: dict[str, Any] = {
            "resourceType": "Observation",
            "status": "final",
            "category": [{"text": section}],
            "code": {"text": item.category or f"{section}-finding"},
            "encounter": {"reference": f"Encounter/{encounter_id}"},
            "valueString": item.statement,
            "identifier": [{"system": IDEMPOTENCY_SYSTEM, "value": ident_val}],
            "note": [
                {
                    "text": (
                        f"polarity={item.polarity or 'PRESENT'}; "
                        f"evidence_ids={','.join(item.evidence_ids)}"
                    )
                }
            ],
        }
        return full_url, resource, f"identifier={IDEMPOTENCY_SYSTEM}|{ident_val}"

    def _build_condition(
        self,
        encounter_id: str,
        item: SoapItem,
        idempotency_key: str,
    ) -> tuple[str, dict[str, Any], str]:
        ident_val = f"{idempotency_key}:condition:{item.item_id}"
        full_url = f"urn:uuid:cond-{item.item_id}"
        if item.polarity == "NEGATED":
            verification_code = "refuted"
            clinical_code = "inactive"
        elif item.polarity == "UNCERTAIN":
            verification_code = "provisional"
            clinical_code = "active"
        else:
            verification_code = "confirmed"
            clinical_code = "active"

        resource: dict[str, Any] = {
            "resourceType": "Condition",
            "clinicalStatus": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                        "code": clinical_code,
                    }
                ]
            },
            "verificationStatus": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                        "code": verification_code,
                    }
                ]
            },
            "code": {"text": item.statement},
            "encounter": {"reference": f"Encounter/{encounter_id}"},
            "identifier": [{"system": IDEMPOTENCY_SYSTEM, "value": ident_val}],
            "note": [{"text": f"evidence_ids={','.join(item.evidence_ids)}"}],
        }
        return full_url, resource, f"identifier={IDEMPOTENCY_SYSTEM}|{ident_val}"

    def _build_medication_resource(
        self,
        encounter_id: str,
        med: MedicationItem,
        idempotency_key: str,
    ) -> tuple[str, dict[str, Any], str]:
        dosage_parts = [
            str(part)
            for part in (med.dose, med.unit, med.route, med.frequency)
            if part is not None
        ]
        dosage_text = " ".join(dosage_parts) if dosage_parts else (med.missing_reason or "")

        if med.medication_status == MedicationStatus.PRESCRIBED:
            ident_val = f"{idempotency_key}:medreq:{med.item_id}"
            full_url = f"urn:uuid:medreq-{med.item_id}"
            resource: dict[str, Any] = {
                "resourceType": "MedicationRequest",
                "status": "active",
                "intent": "order",
                "medicationCodeableConcept": {"text": med.medication_name},
                "encounter": {"reference": f"Encounter/{encounter_id}"},
                "authoredOn": med.event_date.isoformat(),
                "identifier": [{"system": IDEMPOTENCY_SYSTEM, "value": ident_val}],
                "note": [
                    {
                        "text": (
                            f"medication_status={med.medication_status.value}; "
                            f"rationale={med.status_rationale}; "
                            f"evidence_ids={','.join(med.evidence_ids)}"
                        )
                    }
                ],
            }
            if dosage_text:
                resource["dosageInstruction"] = [{"text": dosage_text}]
            return full_url, resource, f"identifier={IDEMPOTENCY_SYSTEM}|{ident_val}"

        # Non-prescribed medication: NEVER create MedicationRequest, NEVER status="active".
        statement_status = _NON_PRESCRIBED_STATEMENT_STATUS[med.medication_status]
        ident_val = f"{idempotency_key}:medstmt:{med.item_id}"
        full_url = f"urn:uuid:medstmt-{med.item_id}"
        resource = {
            "resourceType": "MedicationStatement",
            "status": statement_status,
            "medicationCodeableConcept": {"text": med.medication_name},
            "encounter": {"reference": f"Encounter/{encounter_id}"},
            "effectiveDateTime": med.event_date.isoformat(),
            "identifier": [{"system": IDEMPOTENCY_SYSTEM, "value": ident_val}],
            "note": [
                {
                    "text": (
                        f"medication_status={med.medication_status.value}; "
                        f"rationale={med.status_rationale}; "
                        f"evidence_ids={','.join(med.evidence_ids)}"
                    )
                }
            ],
        }
        if dosage_text:
            resource["dosage"] = [{"text": dosage_text}]
        return full_url, resource, f"identifier={IDEMPOTENCY_SYSTEM}|{ident_val}"

    def commit_signed_soap(
        self,
        tenant_id: str,
        encounter_id: str,
        signed_soap: SoapDraftPayload,
        signer_id: str,
        transcript_ref: str,
        idempotency_key: str,
    ) -> FhirCommitResult:
        """Deterministically commit a clinician-signed SOAP note to FHIR R4."""
        now_iso = datetime.now(timezone.utc).isoformat()
        draft_sha256 = compute_canonical_sha256(signed_soap)

        bundle_entries: list[dict[str, Any]] = []
        clinical_full_urls: list[str] = []
        subj_urls: list[str] = []
        obj_urls: list[str] = []
        assess_urls: list[str] = []
        plan_urls: list[str] = []

        for item in signed_soap.soap.subjective:
            if item.statement is None:
                continue
            full_url, resource, if_none_exist = self._build_observation(
                encounter_id, "subjective", item, idempotency_key
            )
            bundle_entries.append(
                {
                    "fullUrl": full_url,
                    "resource": resource,
                    "request": {"method": "POST", "url": "Observation", "ifNoneExist": if_none_exist},
                }
            )
            clinical_full_urls.append(full_url)
            subj_urls.append(full_url)

        for item in signed_soap.soap.objective:
            if item.statement is None:
                continue
            full_url, resource, if_none_exist = self._build_observation(
                encounter_id, "objective", item, idempotency_key
            )
            bundle_entries.append(
                {
                    "fullUrl": full_url,
                    "resource": resource,
                    "request": {"method": "POST", "url": "Observation", "ifNoneExist": if_none_exist},
                }
            )
            clinical_full_urls.append(full_url)
            obj_urls.append(full_url)

        for item in signed_soap.soap.assessment:
            if item.statement is None:
                continue
            full_url, resource, if_none_exist = self._build_condition(
                encounter_id, item, idempotency_key
            )
            bundle_entries.append(
                {
                    "fullUrl": full_url,
                    "resource": resource,
                    "request": {"method": "POST", "url": "Condition", "ifNoneExist": if_none_exist},
                }
            )
            clinical_full_urls.append(full_url)
            assess_urls.append(full_url)

        for med in signed_soap.soap.plan.medications:
            full_url, resource, if_none_exist = self._build_medication_resource(
                encounter_id, med, idempotency_key
            )
            bundle_entries.append(
                {
                    "fullUrl": full_url,
                    "resource": resource,
                    "request": {
                        "method": "POST",
                        "url": resource["resourceType"],
                        "ifNoneExist": if_none_exist,
                    },
                }
            )
            clinical_full_urls.append(full_url)
            plan_urls.append(full_url)

        plan_narrative_lines = [
            item.statement
            for item in (
                *signed_soap.soap.plan.lifestyle_and_activity,
                *signed_soap.soap.plan.follow_up,
            )
            if item.statement is not None
        ]

        comp_full_url = "urn:uuid:composition-root"
        composition: dict[str, Any] = {
            "resourceType": "Composition",
            "status": "final",
            "type": {"text": "SOAP Note"},
            "title": f"Signed SOAP Note - {encounter_id}",
            "date": now_iso,
            "encounter": {"reference": f"Encounter/{encounter_id}"},
            "author": [{"reference": f"Practitioner/{signer_id}"}],
            "identifier": {"system": IDEMPOTENCY_SYSTEM, "value": idempotency_key},
            "section": [
                {
                    "title": "Subjective",
                    "entry": [{"reference": url} for url in subj_urls],
                },
                {
                    "title": "Objective",
                    "entry": [{"reference": url} for url in obj_urls],
                },
                {
                    "title": "Assessment",
                    "entry": [{"reference": url} for url in assess_urls],
                },
                {
                    "title": "Plan",
                    "entry": [{"reference": url} for url in plan_urls],
                    "text": {
                        "status": "generated",
                        "div": "\n".join(plan_narrative_lines),
                    },
                },
            ],
        }
        bundle_entries.append(
            {
                "fullUrl": comp_full_url,
                "resource": composition,
                "request": {
                    "method": "POST",
                    "url": "Composition",
                    "ifNoneExist": f"identifier={IDEMPOTENCY_SYSTEM}|{idempotency_key}",
                },
            }
        )

        prov_ident_val = f"{idempotency_key}:provenance"
        provenance: dict[str, Any] = {
            "resourceType": "Provenance",
            "recorded": now_iso,
            "target": [
                {"reference": comp_full_url},
                *[{"reference": url} for url in clinical_full_urls],
            ],
            "agent": [{"who": {"reference": f"Practitioner/{signer_id}"}}],
            "entity": [
                {
                    "role": "source",
                    "what": {
                        "display": transcript_ref,
                        "identifier": {
                            "system": "urn:carethread:draft-sha256",
                            "value": draft_sha256,
                        },
                    },
                }
            ],
            "identifier": [{"system": IDEMPOTENCY_SYSTEM, "value": prov_ident_val}],
        }
        bundle_entries.append(
            {
                "fullUrl": "urn:uuid:provenance-root",
                "resource": provenance,
                "request": {
                    "method": "POST",
                    "url": "Provenance",
                    "ifNoneExist": f"identifier={IDEMPOTENCY_SYSTEM}|{prov_ident_val}",
                },
            }
        )

        tx_response = self._store.transaction(
            tenant_id=tenant_id,
            bundle={
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": bundle_entries,
            },
        )

        composition_ref = ""
        provenance_ref = ""
        resource_refs: list[str] = []
        any_created = False

        for entry in tx_response["entry"]:
            res = entry["resource"]
            resp = entry["response"]
            vref = resp["location"]
            resource_refs.append(vref)
            if resp.get("created"):
                any_created = True
            if res["resourceType"] == "Composition":
                composition_ref = vref
            elif res["resourceType"] == "Provenance":
                provenance_ref = vref

        return FhirCommitResult(
            tenant_id=tenant_id,
            encounter_id=encounter_id,
            idempotency_key=idempotency_key,
            composition_ref=composition_ref,
            provenance_ref=provenance_ref,
            resource_refs=resource_refs,
            created_new=any_created,
            draft_sha256=draft_sha256,
        )


__all__ = [
    "DeterministicFhirWriter",
    "FhirCommitResult",
    "FhirPreconditionFailedError",
    "FhirR4Store",
    "FhirResourceNotFoundError",
    "IDEMPOTENCY_SYSTEM",
    "InMemoryFhirR4Store",
]
