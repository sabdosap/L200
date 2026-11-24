"""Model routing primitives for CareThread."""

from carethread.routing.model_router import (
    ALLOWED_THINKING_LEVELS,
    CONFLICT_ESCALATION_MODEL,
    EXTRACTION_MODEL,
    FALLBACK_TRANSCRIPTION_MODEL,
    FLASH_MAX_INPUT_TOKENS,
    FLASH_MAX_OUTPUT_TOKENS,
    MAX_ARGON_ESCALATIONS,
    MAX_DIARIZED_AUDIO_SECONDS,
    PRIMARY_TRANSCRIPTION_MODEL,
    ModelRouteConfig,
    ModelRouter,
    SpeakerDiarizationConfig,
    ThinkingLevel,
    TranscriptionRouteConfig,
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
