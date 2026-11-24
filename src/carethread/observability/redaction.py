"""Fail-closed Sensitive Data Protection (SDP) PHI redaction primitives."""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

DEFAULT_PHI_INFO_TYPES: tuple[str, ...] = (
    "PERSON_NAME",
    "DATE_OF_BIRTH",
    "MEDICAL_RECORD_NUMBER",
    "GCS_AUDIO_PATH",
)

_GCS_AUDIO_PATTERN = re.compile(r"gs://[^\s\"'<>]+")
_MRN_PATTERN = re.compile(r"\bMRN[-:\s]*[A-Za-z0-9-]+\b", re.IGNORECASE)
_DOB_PATTERN = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_TITLED_NAME_PATTERN = re.compile(
    r"\b(Patient|Name|Dr\.?|Mr\.?|Mrs\.?|Ms\.?|Clinician|Provider)(:?\s+)"
    r"([A-Z][a-z]{1,30}(?:\s+[A-Z][a-z]{1,30}){0,2})\b"
)
_BARE_PERSON_NAME_PATTERN = re.compile(
    r"\b[A-Z][a-z]{1,30}(?:\s+[A-Z][a-z]{1,30}){1,2}\b"
)


@runtime_checkable
class SdpClient(Protocol):
    """Protocol for cloud or local Sensitive Data Protection de-identification."""

    def deidentify_text(self, text: str, info_types: tuple[str, ...]) -> str:
        """De-identify PHI matching the requested info_types in free text."""


class LocalRegexSdpClient:
    """Deterministic offline SDP fake covering CareThread's synthetic PHI types."""

    def deidentify_text(self, text: str, info_types: tuple[str, ...]) -> str:
        scrubbed = text
        active_types = set(info_types)

        if "GCS_AUDIO_PATH" in active_types:
            scrubbed = _GCS_AUDIO_PATTERN.sub("[REDACTED:GCS_AUDIO_PATH]", scrubbed)

        if "MEDICAL_RECORD_NUMBER" in active_types:
            scrubbed = _MRN_PATTERN.sub("[REDACTED:MEDICAL_RECORD_NUMBER]", scrubbed)

        if "DATE_OF_BIRTH" in active_types:
            scrubbed = _DOB_PATTERN.sub("[REDACTED:DATE_OF_BIRTH]", scrubbed)

        if "PERSON_NAME" in active_types:
            scrubbed = _TITLED_NAME_PATTERN.sub(
                r"\1\2[REDACTED:PERSON_NAME]", scrubbed
            )
            scrubbed = _BARE_PERSON_NAME_PATTERN.sub(
                "[REDACTED:PERSON_NAME]", scrubbed
            )

        return scrubbed


class FailClosedRedactor:
    """Redactor that drops residual free text if SDP is unavailable or fails."""

    def __init__(
        self,
        sdp_client: SdpClient,
        info_types: tuple[str, ...] = DEFAULT_PHI_INFO_TYPES,
    ) -> None:
        self._sdp_client = sdp_client
        self._info_types = info_types
        self.sdp_unavailable_fail_closed_count: int = 0

    def scrub_free_text(self, text: str) -> tuple[str | None, bool]:
        """Scrub text via SDP, returning (scrubbed_text, failed_closed).

        When SDP raises any error or returns a non-string, increments
        ``sdp_unavailable_fail_closed_count`` and returns ``(None, True)``.
        """
        try:
            scrubbed = self._sdp_client.deidentify_text(text, self._info_types)
        except Exception:
            self.sdp_unavailable_fail_closed_count += 1
            return None, True

        if not isinstance(scrubbed, str):
            self.sdp_unavailable_fail_closed_count += 1
            return None, True

        return scrubbed, False


__all__ = [
    "DEFAULT_PHI_INFO_TYPES",
    "FailClosedRedactor",
    "LocalRegexSdpClient",
    "SdpClient",
]
