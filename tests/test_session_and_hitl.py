"""Tests for durable filtered ADK session state, compaction, and digest-bound HITL confirmations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from carethread.errors import ErrorCategory, GuidedErrorException
from carethread.hitl import (
    ConfirmationStatus,
    HitlConfirmation,
    HitlConfirmationStore,
)
from carethread.schemas.v1.soap import (
    EncounterRef,
    SoapDraftPayload,
    SoapItem,
    SoapPlan,
    SoapSections,
    SourceRefs,
    SpeakerAttribution,
    SpeakerRoleCandidate,
    compute_canonical_sha256,
)
from carethread.session import (
    FilteredSessionStore,
    InMemoryFirestoreBackend,
)


def _make_soap_draft(version: int, complaint_statement: str) -> SoapDraftPayload:
    return SoapDraftPayload(
        draft_id="draft-hitl-001",
        draft_version=version,
        encounter_ref=EncounterRef(
            encounter_id="enc-hitl-001",
            encounter_date=date(2026, 2, 1),
            specialty="primary-care",
        ),
        source_refs=SourceRefs(
            transcript_version_id="gs://carethread-private-artifacts/tenants/t1/transcripts/enc-hitl-001-v1.json",
            fhir_prior_refs=[],
        ),
        speaker_attribution=SpeakerAttribution(
            status="CLINICIAN_CONFIRMED",
            candidates=[
                SpeakerRoleCandidate(diarization_label="SPEAKER_0", proposed_role="CLINICIAN"),
                SpeakerRoleCandidate(diarization_label="SPEAKER_1", proposed_role="PATIENT"),
            ],
        ),
        soap=SoapSections(
            subjective=[
                SoapItem(
                    item_id="subj-1",
                    category="chief-complaint",
                    statement=complaint_statement,
                    polarity="PRESENT",
                    speaker_role="PATIENT",
                    evidence_ids=["ev-001"],
                )
            ],
            objective=[],
            assessment=[],
            plan=SoapPlan(),
        ),
    )


@pytest.mark.parametrize(
    "forbidden_key,value",
    [
        ("transcript_text", "Patient says my foot burns at night."),
        ("audio_gcs_path", "gs://carethread-private-artifacts/tenants/t1/audio/enc-1.wav"),
        ("patient_name", "Jane Doe"),
        ("raw_prompt", "System prompt with raw clinical context"),
        ("raw_model_output", "{\"raw\": \"model output\"}"),
    ],
)
def test_session_rejects_forbidden_phi_and_raw_keys(forbidden_key: str, value: str) -> None:
    backend = InMemoryFirestoreBackend()
    store = FilteredSessionStore(backend=backend)
    now = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        store.create_session(
            session_id="sess-phi-create",
            tenant_id="tenant-a",
            user_id="clinician-1",
            state={forbidden_key: value},
            now=now,
        )

    store.create_session(
        session_id="sess-phi-update",
        tenant_id="tenant-a",
        user_id="clinician-1",
        state={"encounter_id": "enc-001"},
        now=now,
    )

    with pytest.raises(ValueError):
        store.update_state(
            "sess-phi-update",
            {forbidden_key: value},
            now=now,
        )

    # Also block smuggling forbidden keys inside nested allowed metadata keys
    with pytest.raises(ValueError):
        store.update_state(
            "sess-phi-update",
            {"metadata": {forbidden_key: value}},
            now=now,
        )


def test_session_rejects_unknown_keys_not_in_allowlist() -> None:
    store = FilteredSessionStore()
    now = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    store.create_session(
        session_id="sess-allowlist",
        tenant_id="tenant-a",
        user_id="clinician-1",
        now=now,
    )

    with pytest.raises(ValueError):
        store.update_state("sess-allowlist", {"arbitrary_unvetted_field": "x"}, now=now)


def test_compaction_trims_to_limits_while_preserving_open_hitl_confirmations() -> None:
    backend = InMemoryFirestoreBackend()
    store = FilteredSessionStore(backend=backend)
    base = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)

    store.create_session(
        session_id="sess-compact",
        tenant_id="tenant-a",
        user_id="clinician-1",
        state={
            "encounter_id": "enc-100",
            "requested_tool_confirmations": {
                "call-hitl-sign": {
                    "confirmation_id": "conf-001",
                    "kind": "SOAP_SIGN_OFF",
                    "artifact_id": "draft-hitl-001",
                    "artifact_version": 1,
                }
            },
        },
        now=base,
    )

    # Append 20 turns; turn 2 carries the open requested_tool_confirmations call
    for turn in range(20):
        ts = base + timedelta(minutes=turn)
        if turn == 2:
            store.append_event(
                session_id="sess-compact",
                role="model",
                content_summary="Requested clinician sign-off HITL confirmation for draft v1",
                token_count=600,
                function_call_id="call-hitl-sign",
                is_function_call=True,
                requested_tool_confirmations={
                    "call-hitl-sign": {
                        "confirmation_id": "conf-001",
                        "kind": "SOAP_SIGN_OFF",
                    }
                },
                now=ts,
            )
        else:
            store.append_event(
                session_id="sess-compact",
                role="user" if turn % 2 == 0 else "model",
                content_summary=f"Metadata turn {turn}",
                token_count=500,
                now=ts,
            )

    compacted = store.compact_events(
        session_id="sess-compact",
        max_turns=6,
        max_tokens=16000,
        now=base + timedelta(minutes=21),
    )

    assert len(compacted.events) <= 6
    assert sum(event.token_count for event in compacted.events) <= 16000
    assert any(
        "call-hitl-sign" in event.requested_tool_confirmations
        for event in compacted.events
    )
    assert "call-hitl-sign" in compacted.state["requested_tool_confirmations"]
    # Most recent turns should be kept in chronological order alongside the protected turn
    kept_turns = [event.turn_index for event in compacted.events]
    assert kept_turns == sorted(kept_turns)
    assert 2 in kept_turns
    assert 19 in kept_turns


def test_compaction_respects_token_cap_while_preserving_unresolved_function_call() -> None:
    store = FilteredSessionStore()
    base = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    store.create_session(
        session_id="sess-tokens",
        tenant_id="tenant-a",
        user_id="clinician-1",
        now=base,
    )

    store.append_event(
        session_id="sess-tokens",
        role="model",
        content_summary="Unresolved tool call waiting on HITL response",
        token_count=4000,
        function_call_id="call-unresolved-1",
        is_function_call=True,
        now=base,
    )
    for i in range(1, 8):
        store.append_event(
            session_id="sess-tokens",
            role="user",
            content_summary=f"Follow-up status check {i}",
            token_count=3500,
            now=base + timedelta(minutes=i),
        )

    compacted = store.compact_events(
        session_id="sess-tokens",
        max_turns=6,
        max_tokens=10000,
        now=base + timedelta(minutes=10),
    )

    assert sum(event.token_count for event in compacted.events) <= 10000
    assert any(event.function_call_id == "call-unresolved-1" for event in compacted.events)


def test_fresh_session_store_survives_simulated_restart() -> None:
    shared_backend = InMemoryFirestoreBackend()
    now = datetime(2026, 2, 1, 14, 0, tzinfo=timezone.utc)

    store_before_restart = FilteredSessionStore(backend=shared_backend)
    store_before_restart.create_session(
        session_id="sess-restart-1",
        tenant_id="tenant-a",
        user_id="clinician-1",
        state={
            "encounter_id": "enc-restart-1",
            "draft_id": "draft-1",
            "draft_version": 1,
            "requested_tool_confirmations": {
                "call-restart": {
                    "confirmation_id": "conf-restart-1",
                    "kind": "SOAP_SIGN_OFF",
                }
            },
        },
        now=now,
    )
    store_before_restart.append_event(
        session_id="sess-restart-1",
        role="model",
        content_summary="Awaiting clinician review confirmation",
        token_count=120,
        function_call_id="call-restart",
        is_function_call=True,
        requested_tool_confirmations={"call-restart": {"confirmation_id": "conf-restart-1"}},
        now=now + timedelta(seconds=5),
    )

    # Simulate Cloud Run cold restart with a brand new store instance over same backend
    store_after_restart = FilteredSessionStore(backend=shared_backend)
    restored = store_after_restart.get_session("sess-restart-1")

    assert restored.tenant_id == "tenant-a"
    assert restored.state["encounter_id"] == "enc-restart-1"
    assert restored.state["requested_tool_confirmations"]["call-restart"]["confirmation_id"] == "conf-restart-1"
    assert len(restored.events) == 1
    assert restored.events[0].function_call_id == "call-restart"


def test_hitl_toctou_digest_race_supersedes_v1_and_raises_digest_mismatch() -> None:
    backend = InMemoryFirestoreBackend()
    hitl = HitlConfirmationStore(backend=backend)
    now = datetime(2026, 2, 1, 15, 0, tzinfo=timezone.utc)

    draft_v1 = _make_soap_draft(version=1, complaint_statement="Burning foot pain at night.")
    digest_v1 = compute_canonical_sha256(draft_v1)

    conf_v1 = hitl.create_confirmation(
        artifact_id=draft_v1.draft_id,
        artifact_version=1,
        sha256_digest=digest_v1,
        kind="SOAP_SIGN_OFF",
        ttl_seconds=600,
        now=now,
    )
    assert isinstance(conf_v1, HitlConfirmation)
    assert conf_v1.status == ConfirmationStatus.PENDING

    # Clinician edits draft before stale tab approval arrives
    draft_v2 = _make_soap_draft(version=2, complaint_statement="Burning foot pain at night for 3 weeks.")
    digest_v2 = compute_canonical_sha256(draft_v2)

    superseded = hitl.supersede_for_artifact(
        artifact_id=draft_v1.draft_id,
        new_version=2,
        new_digest=digest_v2,
        now=now + timedelta(seconds=10),
    )
    assert len(superseded) == 1
    assert superseded[0].confirmation_id == conf_v1.confirmation_id
    assert superseded[0].status == ConfirmationStatus.INVALIDATED_SUPERSEDED

    # Stale client attempts to approve v1
    with pytest.raises(GuidedErrorException) as exc_info:
        hitl.approve(
            confirmation_id=conf_v1.confirmation_id,
            actor_id="clinician-1",
            expected_version=1,
            expected_digest=digest_v1,
            now=now + timedelta(seconds=15),
        )
    assert exc_info.value.envelope.category == ErrorCategory.HITL_DIGEST_MISMATCH

    # Wrong digest or version against an active confirmation also fails with HITL_DIGEST_MISMATCH
    conf_v2 = hitl.create_confirmation(
        artifact_id=draft_v2.draft_id,
        artifact_version=2,
        sha256_digest=digest_v2,
        kind="SOAP_SIGN_OFF",
        ttl_seconds=600,
        now=now + timedelta(seconds=20),
    )
    with pytest.raises(GuidedErrorException) as mismatch_exc:
        hitl.approve(
            confirmation_id=conf_v2.confirmation_id,
            actor_id="clinician-1",
            expected_version=2,
            expected_digest=digest_v1,  # wrong digest
            now=now + timedelta(seconds=25),
        )
    assert mismatch_exc.value.envelope.category == ErrorCategory.HITL_DIGEST_MISMATCH

    approved_v2 = hitl.approve(
        confirmation_id=conf_v2.confirmation_id,
        actor_id="clinician-1",
        expected_version=2,
        expected_digest=digest_v2,
        now=now + timedelta(seconds=30),
    )
    assert approved_v2.status == ConfirmationStatus.APPROVED
    assert approved_v2.decided_by == "clinician-1"


def test_hitl_replay_expiry_and_rejection_fail_closed() -> None:
    backend = InMemoryFirestoreBackend()
    hitl = HitlConfirmationStore(backend=backend)
    now = datetime(2026, 2, 1, 16, 0, tzinfo=timezone.utc)

    draft = _make_soap_draft(version=1, complaint_statement="Foot pain.")
    digest = compute_canonical_sha256(draft)

    # 1. Single-use approval token cannot be replayed
    conf_single = hitl.create_confirmation(
        artifact_id="art-single",
        artifact_version=1,
        sha256_digest=digest,
        kind="SOAP_SIGN_OFF",
        ttl_seconds=300,
        now=now,
    )
    hitl.approve(
        confirmation_id=conf_single.confirmation_id,
        actor_id="clinician-1",
        expected_version=1,
        expected_digest=digest,
        now=now + timedelta(seconds=5),
    )
    with pytest.raises(GuidedErrorException):
        hitl.approve(
            confirmation_id=conf_single.confirmation_id,
            actor_id="clinician-1",
            expected_version=1,
            expected_digest=digest,
            now=now + timedelta(seconds=10),
        )

    # 2. Approving past expires_at fails closed
    conf_expiring = hitl.create_confirmation(
        artifact_id="art-expire",
        artifact_version=1,
        sha256_digest=digest,
        kind="INSURER_RELEASE",
        ttl_seconds=60,
        now=now,
    )
    with pytest.raises(GuidedErrorException) as exp_exc:
        hitl.approve(
            confirmation_id=conf_expiring.confirmation_id,
            actor_id="clinician-1",
            expected_version=1,
            expected_digest=digest,
            now=now + timedelta(seconds=61),
        )
    assert exp_exc.value.envelope.category == ErrorCategory.GRANT_EXPIRED

    # 3. Rejected confirmation cannot subsequently be approved
    conf_reject = hitl.create_confirmation(
        artifact_id="art-reject",
        artifact_version=1,
        sha256_digest=digest,
        kind="SPEAKER_ATTRIBUTION",
        ttl_seconds=300,
        now=now,
    )
    rejected = hitl.reject(
        confirmation_id=conf_reject.confirmation_id,
        actor_id="clinician-1",
        reason="Speaker labels swapped",
        now=now + timedelta(seconds=5),
    )
    assert rejected.status == ConfirmationStatus.REJECTED
    assert rejected.rejection_reason == "Speaker labels swapped"

    with pytest.raises(GuidedErrorException):
        hitl.approve(
            confirmation_id=conf_reject.confirmation_id,
            actor_id="clinician-1",
            expected_version=1,
            expected_digest=digest,
            now=now + timedelta(seconds=10),
        )
