from __future__ import annotations

from typing import Any

from ..config import Settings
from .base import ASREventHandler, StreamingASRSession
from .disabled import DisabledASRSession
from .tencent import TencentRealtimeASRSession


def asr_capability(settings: Settings) -> dict[str, Any]:
    return {
        "provider": settings.asr_provider,
        "configured": settings.asr_configured,
        "status": "ready" if settings.asr_configured else "not_configured",
        "engine_model_type": settings.asr_engine_model_type if settings.asr_provider != "disabled" else None,
        "speaker_diarization": settings.asr_configured and "speaker" in settings.asr_engine_model_type,
        "interim_results": settings.asr_configured,
        "final_results": settings.asr_configured,
    }


def create_asr_session(
    settings: Settings, event_handler: ASREventHandler
) -> StreamingASRSession:
    if not settings.asr_configured:
        return DisabledASRSession()
    if settings.asr_provider == "tencent":
        return TencentRealtimeASRSession(
            app_id=settings.tencent_asr_app_id or "",
            secret_id=settings.tencent_asr_secret_id or "",
            secret_key=settings.tencent_asr_secret_key or "",
            engine_model_type=settings.asr_engine_model_type,
            hotwords=settings.tencent_asr_hotwords,
            event_handler=event_handler,
        )
    return DisabledASRSession()
