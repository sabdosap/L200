"""Strategic model routing for transcription, extraction/brief, and gated conflict escalation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeVar
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from carethread.errors import ErrorCategory, GuidedErrorEnvelope, GuidedErrorException
from carethread.schemas import StrictModel

T = TypeVar("T")

PRIMARY_TRANSCRIPTION_MODEL = "gemini-3.5-transcribe"
FALLBACK_TRANSCRIPTION_MODEL = "chirp_3"
EXTRACTION_MODEL = "gemini-3.8-flash"
CONFLICT_ESCALATION_MODEL = "gemini-4-argon"

MAX_DIARIZED_AUDIO_SECONDS: int = 1800
MAX_ARGON_ESCALATIONS: int = 1
ALLOWED_THINKING_LEVELS: frozenset[str] = frozenset({"LOW", "MEDIUM", "HIGH"})
FLASH_MAX_INPUT_TOKENS: int = 1_048_576
FLASH_MAX_OUTPUT_TOKENS: int = 65_536

ThinkingLevel = Literal["LOW", "MEDIUM", "HIGH"]


class SpeakerDiarizationConfig(StrictModel):
    """Explicit speaker diarization configuration for Chirp 3 BatchRecognize fallback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_speaker_count: int = Field(default=2, ge=1)
    max_speaker_count: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def _validate_speaker_bounds(self) -> SpeakerDiarizationConfig:
        if self.max_speaker_count < self.min_speaker_count:
            raise ValueError("max_speaker_count must be >= min_speaker_count")
        return self


class TranscriptionRouteConfig(StrictModel):
    """Resolved transcription model and configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: Literal["gemini-3.5-transcribe", "chirp_3"]
    mode: Literal["VERBATIM"] = "VERBATIM"
    api_operation: Literal["GenerateContent", "BatchRecognize"]
    diarization: bool = True
    word_timestamps: bool = True
    is_fallback: bool = False
    audio_duration_seconds: float = Field(gt=0)
    speaker_diarization_config: SpeakerDiarizationConfig | None = None


class ModelRouteConfig(StrictModel):
    """Resolved structured generation model and thinking configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: Literal["gemini-3.8-flash", "gemini-4-argon"]
    task: str = Field(min_length=1)
    thinking_level: ThinkingLevel
    response_schema: dict[str, Any]
    max_input_tokens: int = Field(default=FLASH_MAX_INPUT_TOKENS, gt=0)
    max_output_tokens: int = Field(default=FLASH_MAX_OUTPUT_TOKENS, gt=0)
    escalated_to_argon: bool = False


def _normalize_response_schema(
    response_schema: type[BaseModel] | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
        schema = response_schema.model_json_schema()
    elif isinstance(response_schema, Mapping):
        schema = dict(response_schema)
    else:
        raise ValueError("response_schema must be a Pydantic BaseModel subclass or JSON schema mapping")

    if not schema:
        raise ValueError("response_schema must be non-empty")
    return schema


def _validate_thinking_level(thinking_level: str) -> ThinkingLevel:
    if thinking_level not in ALLOWED_THINKING_LEVELS:
        raise ValueError(
            f"Unsupported thinking_level '{thinking_level}'. Allowed levels: {sorted(ALLOWED_THINKING_LEVELS)}"
        )
    return thinking_level  # type: ignore[return-value]


class ModelRouter:
    """Deterministic model router enforcing audio caps, Flash schema defaults, and gated Argon escalation."""

    def __init__(
        self,
        enable_argon_second_opinion: bool = False,
        *,
        max_audio_duration_seconds: int = MAX_DIARIZED_AUDIO_SECONDS,
        max_argon_escalations: int = MAX_ARGON_ESCALATIONS,
    ) -> None:
        if max_audio_duration_seconds <= 0:
            raise ValueError("max_audio_duration_seconds must be positive")
        if max_argon_escalations < 0:
            raise ValueError("max_argon_escalations must be >= 0")
        self._enable_argon_second_opinion = bool(enable_argon_second_opinion)
        self._max_audio_duration_seconds = max_audio_duration_seconds
        self._max_argon_escalations = max_argon_escalations

    @property
    def enable_argon_second_opinion(self) -> bool:
        return self._enable_argon_second_opinion

    def route_transcription(
        self,
        audio_duration_seconds: float,
        *,
        diarization: bool = True,
        word_timestamps: bool = True,
        primary_failed: bool = False,
        min_speaker_count: int = 2,
        max_speaker_count: int = 2,
        trace_id: str | None = None,
    ) -> TranscriptionRouteConfig:
        """Select primary gemini-3.5-transcribe or chirp_3 BatchRecognize fallback with 30-min cap check."""
        resolved_trace = trace_id or uuid.uuid4().hex
        if audio_duration_seconds <= 0:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="INVALID_AUDIO_DURATION",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="audio_duration_seconds",
                    guided_recovery="Provide a positive audio duration in seconds before starting transcription.",
                    trace_id=resolved_trace,
                )
            )

        if (diarization or word_timestamps) and audio_duration_seconds > self._max_audio_duration_seconds:
            raise GuidedErrorException(
                GuidedErrorEnvelope(
                    error_code="AUDIO_DURATION_EXCEEDS_DIARIZATION_CAP",
                    category=ErrorCategory.VALIDATION_RECOVERABLE,
                    retryable=False,
                    max_retries_remaining=0,
                    field_path="audio_duration_seconds",
                    guided_recovery=(
                        "Audio with diarization or word timestamps must not exceed 1800 seconds (30 minutes); "
                        "split the recording into segments <= 30 minutes."
                    ),
                    trace_id=resolved_trace,
                )
            )

        diarization_cfg = (
            SpeakerDiarizationConfig(
                min_speaker_count=min_speaker_count,
                max_speaker_count=max_speaker_count,
            )
            if diarization
            else None
        )

        if primary_failed:
            return TranscriptionRouteConfig(
                model_id=FALLBACK_TRANSCRIPTION_MODEL,
                mode="VERBATIM",
                api_operation="BatchRecognize",
                diarization=diarization,
                word_timestamps=word_timestamps,
                is_fallback=True,
                audio_duration_seconds=audio_duration_seconds,
                speaker_diarization_config=diarization_cfg,
            )

        return TranscriptionRouteConfig(
            model_id=PRIMARY_TRANSCRIPTION_MODEL,
            mode="VERBATIM",
            api_operation="GenerateContent",
            diarization=diarization,
            word_timestamps=word_timestamps,
            is_fallback=False,
            audio_duration_seconds=audio_duration_seconds,
            speaker_diarization_config=diarization_cfg,
        )

    def transcribe_with_fallback(
        self,
        audio_duration_seconds: float,
        primary_fn: Callable[[TranscriptionRouteConfig], T],
        fallback_fn: Callable[[TranscriptionRouteConfig], T],
        *,
        diarization: bool = True,
        word_timestamps: bool = True,
        min_speaker_count: int = 2,
        max_speaker_count: int = 2,
        trace_id: str | None = None,
    ) -> tuple[T, TranscriptionRouteConfig]:
        """Attempt primary gemini-3.5-transcribe and deterministically fall back to chirp_3 BatchRecognize."""
        primary_cfg = self.route_transcription(
            audio_duration_seconds=audio_duration_seconds,
            diarization=diarization,
            word_timestamps=word_timestamps,
            primary_failed=False,
            min_speaker_count=min_speaker_count,
            max_speaker_count=max_speaker_count,
            trace_id=trace_id,
        )
        try:
            return primary_fn(primary_cfg), primary_cfg
        except GuidedErrorException:
            raise
        except Exception:
            fallback_cfg = self.route_transcription(
                audio_duration_seconds=audio_duration_seconds,
                diarization=diarization,
                word_timestamps=word_timestamps,
                primary_failed=True,
                min_speaker_count=min_speaker_count,
                max_speaker_count=max_speaker_count,
                trace_id=trace_id,
            )
            return fallback_fn(fallback_cfg), fallback_cfg

    def route_extraction(
        self,
        *,
        response_schema: type[BaseModel] | Mapping[str, Any],
        thinking_level: ThinkingLevel = "MEDIUM",
        task: str = "soap_extraction",
    ) -> ModelRouteConfig:
        """Select gemini-3.8-flash with strict response_schema and valid thinking_level."""
        validated_thinking = _validate_thinking_level(thinking_level)
        normalized_schema = _normalize_response_schema(response_schema)
        return ModelRouteConfig(
            model_id=EXTRACTION_MODEL,
            task=task,
            thinking_level=validated_thinking,
            response_schema=normalized_schema,
            max_input_tokens=FLASH_MAX_INPUT_TOKENS,
            max_output_tokens=FLASH_MAX_OUTPUT_TOKENS,
            escalated_to_argon=False,
        )

    def route_brief(
        self,
        *,
        response_schema: type[BaseModel] | Mapping[str, Any],
        thinking_level: ThinkingLevel = "MEDIUM",
    ) -> ModelRouteConfig:
        """Select gemini-3.8-flash for continuity brief generation."""
        return self.route_extraction(
            response_schema=response_schema,
            thinking_level=thinking_level,
            task="continuity_brief",
        )

    def can_escalate_to_argon(self, escalation_count: int = 0) -> bool:
        """Return True only when Argon second opinion is enabled and under the hard cap."""
        if escalation_count < 0:
            raise ValueError("escalation_count must be >= 0")
        return self._enable_argon_second_opinion and escalation_count < self._max_argon_escalations

    def route_conflict_escalation(
        self,
        *,
        response_schema: type[BaseModel] | Mapping[str, Any],
        escalation_count: int = 0,
        thinking_level: ThinkingLevel = "HIGH",
    ) -> ModelRouteConfig:
        """Select gemini-4-argon only when enabled and escalation_count == 0; otherwise stay on Flash."""
        validated_thinking = _validate_thinking_level(thinking_level)
        normalized_schema = _normalize_response_schema(response_schema)
        if self.can_escalate_to_argon(escalation_count):
            return ModelRouteConfig(
                model_id=CONFLICT_ESCALATION_MODEL,
                task="conflict_escalation",
                thinking_level=validated_thinking,
                response_schema=normalized_schema,
                max_input_tokens=FLASH_MAX_INPUT_TOKENS,
                max_output_tokens=FLASH_MAX_OUTPUT_TOKENS,
                escalated_to_argon=True,
            )

        return ModelRouteConfig(
            model_id=EXTRACTION_MODEL,
            task="conflict_escalation",
            thinking_level=validated_thinking,
            response_schema=normalized_schema,
            max_input_tokens=FLASH_MAX_INPUT_TOKENS,
            max_output_tokens=FLASH_MAX_OUTPUT_TOKENS,
            escalated_to_argon=False,
        )


__all__ = [
    "ALLOWED_THINKING_LEVELS",
    "CONFLICT_ESCALATION_MODEL",
    "EXTRACTION_MODEL",
    "FALLBACK_TRANSCRIPTION_MODEL",
    "FLASH_MAX_INPUT_TOKENS",
    "FLASH_MAX_OUTPUT_TOKENS",
    "MAX_ARGON_ESCALATIONS",
    "MAX_DIARIZED_AUDIO_SECONDS",
    "ModelRouteConfig",
    "ModelRouter",
    "PRIMARY_TRANSCRIPTION_MODEL",
    "SpeakerDiarizationConfig",
    "ThinkingLevel",
    "TranscriptionRouteConfig",
]
