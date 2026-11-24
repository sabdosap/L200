from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json

import pytest

from carethread.errors import ErrorCategory, GuidedErrorException
from carethread.security import (
    ALLOWED_INSURER_PURPOSES,
    ActorContext,
    AuthzService,
    ConsentRecord,
    ConsentStatus,
    GrantService,
    InMemorySecretManagerClient,
    ReleaseGrant,
    ScopeCapabilityToken,
    SecretManagerClient,
)

PROJECT_ID = "carethread-test-project"
SECRET_ID = "scope-token-hmac-key"
SECRET_VERSION = "1"
SIGNING_KEY = b"test-only-hmac-key-material-32bytes"


def _make_secret_manager() -> InMemorySecretManagerClient:
    return InMemorySecretManagerClient(
        secrets={(PROJECT_ID, SECRET_ID, SECRET_VERSION): SIGNING_KEY}
    )


def _make_authz_service(secret_manager: SecretManagerClient | None = None) -> AuthzService:
    return AuthzService(
        secret_manager=secret_manager or _make_secret_manager(),
        secret_project_id=PROJECT_ID,
        secret_id=SECRET_ID,
        secret_version=SECRET_VERSION,
    )


def _now() -> datetime:
    return datetime(2026, 3, 31, 10, 0, 0, tzinfo=timezone.utc)


def _active_consent(
    *,
    tenant_id: str = "tenant-a",
    patient_id: str = "PT-SYN-0001",
    consent_version_id: str = "CV-2026-01",
    status: str = "ACTIVE",
) -> ConsentRecord:
    return ConsentRecord(
        consent_version_id=consent_version_id,
        tenant_id=tenant_id,
        patient_id=patient_id,
        status=status,
        allowed_categories=frozenset({"clinical_summary", "medications", "conditions", "provenance"}),
    )


def test_cross_tenant_access_raises_authz_denied():
    authz = _make_authz_service()
    actor = ActorContext(
        actor_id="CLIN-B-01",
        tenant_id="tenant-b",
        role="CLINICIAN",
        specialty="orthopedics",
        care_patient_ids=frozenset({"PT-SYN-0001"}),
    )
    consent = _active_consent(tenant_id="tenant-a", patient_id="PT-SYN-0001")

    with pytest.raises(GuidedErrorException) as exc_info:
        authz.authorize_clinical_access(
            actor=actor,
            patient_id="PT-SYN-0001",
            patient_tenant_id="tenant-a",
            consent_record=consent,
            now=_now(),
        )

    assert exc_info.value.envelope.category == ErrorCategory.AUTHZ_DENIED
    assert exc_info.value.envelope.retryable is False


def test_same_tenant_without_care_relationship_raises_authz_denied():
    authz = _make_authz_service()
    actor = ActorContext(
        actor_id="CLIN-A-99",
        tenant_id="tenant-a",
        role="CLINICIAN",
        specialty="orthopedics",
        care_patient_ids=frozenset({"PT-SYN-9999"}),
    )
    consent = _active_consent(tenant_id="tenant-a", patient_id="PT-SYN-0001")

    with pytest.raises(GuidedErrorException) as exc_info:
        authz.authorize_clinical_access(
            actor=actor,
            patient_id="PT-SYN-0001",
            patient_tenant_id="tenant-a",
            consent_record=consent,
            now=_now(),
        )

    assert exc_info.value.envelope.category == ErrorCategory.AUTHZ_DENIED


def test_disallowed_role_raises_authz_denied():
    authz = _make_authz_service()
    actor = ActorContext(
        actor_id="INS-01",
        tenant_id="tenant-a",
        role="INSURER",
        specialty=None,
        care_patient_ids=frozenset({"PT-SYN-0001"}),
    )
    consent = _active_consent(tenant_id="tenant-a", patient_id="PT-SYN-0001")

    with pytest.raises(GuidedErrorException) as exc_info:
        authz.authorize_clinical_access(
            actor=actor,
            patient_id="PT-SYN-0001",
            patient_tenant_id="tenant-a",
            consent_record=consent,
            now=_now(),
        )

    assert exc_info.value.envelope.category == ErrorCategory.AUTHZ_DENIED


def test_revoked_consent_raises_consent_revoked():
    authz = _make_authz_service()
    actor = ActorContext(
        actor_id="CLIN-A-02",
        tenant_id="tenant-a",
        role="CLINICIAN",
        specialty="orthopedics",
        care_patient_ids=frozenset({"PT-SYN-0001"}),
    )
    consent = _active_consent(status="REVOKED")

    with pytest.raises(GuidedErrorException) as exc_info:
        authz.authorize_clinical_access(
            actor=actor,
            patient_id="PT-SYN-0001",
            patient_tenant_id="tenant-a",
            consent_record=consent,
            now=_now(),
        )

    assert exc_info.value.envelope.category == ErrorCategory.CONSENT_REVOKED
    assert exc_info.value.envelope.retryable is False


def test_authorized_access_issues_hmac_signed_token_and_verifies():
    authz = _make_authz_service()
    now = _now()
    actor = ActorContext(
        actor_id="CLIN-A-02",
        tenant_id="tenant-a",
        role="CLINICIAN",
        specialty="orthopedics",
        care_patient_ids=frozenset({"PT-SYN-0001"}),
    )
    consent = _active_consent()

    token = authz.authorize_clinical_access(
        actor=actor,
        patient_id="PT-SYN-0001",
        patient_tenant_id="tenant-a",
        consent_record=consent,
        now=now,
    )

    assert isinstance(token, ScopeCapabilityToken)
    assert token.tenant_id == "tenant-a"
    assert token.patient_id == "PT-SYN-0001"
    assert token.actor_id == "CLIN-A-02"
    assert token.consent_version_id == "CV-2026-01"
    assert token.allowed_categories == consent.allowed_categories
    assert token.expires_at > now

    expected_payload = json.dumps(
        {
            "schema_version": token.schema_version,
            "token_id": token.token_id,
            "tenant_id": token.tenant_id,
            "patient_id": token.patient_id,
            "actor_id": token.actor_id,
            "allowed_categories": sorted(token.allowed_categories),
            "consent_version_id": token.consent_version_id,
            "expires_at": token.expires_at.astimezone(timezone.utc).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    expected_sig = hmac.new(SIGNING_KEY, expected_payload, hashlib.sha256).hexdigest()
    assert token.signature == expected_sig

    assert authz.verify_token(token, now + timedelta(minutes=1)) is True
    assert authz.verify_token(token, token.expires_at) is False
    assert authz.verify_token(token, token.expires_at + timedelta(seconds=1)) is False

    tampered_patient = token.model_copy(update={"patient_id": "PT-SYN-OTHER"})
    assert authz.verify_token(tampered_patient, now) is False

    tampered_sig = token.model_copy(update={"signature": "0" * 64})
    assert authz.verify_token(tampered_sig, now) is False


def test_out_of_scope_insurer_purpose_raises_out_of_scope_purpose():
    assert ALLOWED_INSURER_PURPOSES == frozenset({"prior_auth_documentation"})
    grant_service = GrantService()
    consent = _active_consent()
    now = _now()

    for bad_purpose in ("underwriting_risk_score", "claims_adjudication"):
        with pytest.raises(GuidedErrorException) as exc_info:
            grant_service.create_grant(
                tenant_id="tenant-a",
                patient_id="PT-SYN-0001",
                insurer_id="INS-SYN-01",
                purpose=bad_purpose,
                consent_record=consent,
                now=now,
            )
        assert exc_info.value.envelope.category == ErrorCategory.OUT_OF_SCOPE_PURPOSE


def test_grant_expiry_and_consent_version_change_fail_closed():
    grant_service = GrantService()
    now = _now()
    consent_v1 = _active_consent(consent_version_id="CV-2026-01")

    grant = grant_service.create_grant(
        tenant_id="tenant-a",
        patient_id="PT-SYN-0001",
        insurer_id="INS-SYN-01",
        purpose="prior_auth_documentation",
        consent_record=consent_v1,
        now=now,
    )
    assert isinstance(grant, ReleaseGrant)
    assert grant.expires_at == now + timedelta(hours=72)
    assert grant.consent_version_id == "CV-2026-01"

    verified = grant_service.verify_grant_active(grant, consent_v1, now + timedelta(hours=1))
    assert verified.grant_id == grant.grant_id

    # Past expires_at raises GRANT_EXPIRED
    with pytest.raises(GuidedErrorException) as expired_exc:
        grant_service.verify_grant_active(grant, consent_v1, now + timedelta(hours=72, seconds=1))
    assert expired_exc.value.envelope.category == ErrorCategory.GRANT_EXPIRED

    # Changing patient consent version invalidates grant with CONSENT_REVOKED
    consent_v2 = _active_consent(consent_version_id="CV-2026-02")
    with pytest.raises(GuidedErrorException) as version_exc:
        grant_service.verify_grant_active(grant, consent_v2, now + timedelta(hours=1))
    assert version_exc.value.envelope.category == ErrorCategory.CONSENT_REVOKED

    # Revoking consent also raises CONSENT_REVOKED
    consent_revoked = _active_consent(consent_version_id="CV-2026-01", status=ConsentStatus.REVOKED)
    with pytest.raises(GuidedErrorException) as revoked_exc:
        grant_service.verify_grant_active(grant, consent_revoked, now + timedelta(hours=1))
    assert revoked_exc.value.envelope.category == ErrorCategory.CONSENT_REVOKED


def test_secret_manager_resolves_versioned_secret_and_has_no_fallback():
    client = _make_secret_manager()
    assert isinstance(client, SecretManagerClient)
    assert client.get_secret(PROJECT_ID, SECRET_ID, SECRET_VERSION) == SIGNING_KEY

    with pytest.raises(KeyError):
        client.get_secret(PROJECT_ID, SECRET_ID, "999")

    with pytest.raises(KeyError):
        client.get_secret(PROJECT_ID, "nonexistent-secret", "1")

    with pytest.raises(KeyError):
        client.get_secret("other-project", SECRET_ID, SECRET_VERSION)

    # AuthzService must not silently fall back to a hardcoded signing key
    empty_client = InMemorySecretManagerClient()
    authz = _make_authz_service(secret_manager=empty_client)
    actor = ActorContext(
        actor_id="CLIN-A-02",
        tenant_id="tenant-a",
        role="CLINICIAN",
        specialty="orthopedics",
        care_patient_ids=frozenset({"PT-SYN-0001"}),
    )
    with pytest.raises(KeyError):
        authz.authorize_clinical_access(
            actor=actor,
            patient_id="PT-SYN-0001",
            patient_tenant_id="tenant-a",
            consent_record=_active_consent(),
            now=_now(),
        )
