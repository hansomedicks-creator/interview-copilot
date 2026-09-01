from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_path_env(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value and value.strip() else None


@dataclass(frozen=True)
class Settings:
    environment: str = field(
        default_factory=lambda: os.getenv("INTERVIEW_ENV", "development")
    )
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "INTERVIEW_DATABASE_URL", "sqlite:///./interview-copilot.db"
        )
    )
    provider_mode: str = field(
        default_factory=lambda: os.getenv("INTERVIEW_PROVIDER_MODE", "mock")
    )
    llm_base_url: str = field(
        default_factory=lambda: os.getenv("INTERVIEW_LLM_BASE_URL", "https://api.openai.com/v1")
    )
    llm_api_key: str | None = field(
        default_factory=lambda: os.getenv("INTERVIEW_LLM_API_KEY") or None
    )
    llm_model: str | None = field(
        default_factory=lambda: os.getenv("INTERVIEW_LLM_MODEL") or None
    )
    llm_planning_model: str | None = field(
        default_factory=lambda: os.getenv("INTERVIEW_LLM_PLANNING_MODEL") or None
    )
    llm_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("INTERVIEW_LLM_TIMEOUT_SECONDS", "12"))
    )
    llm_max_context_chars: int = field(
        default_factory=lambda: int(os.getenv("INTERVIEW_LLM_MAX_CONTEXT_CHARS", "12000"))
    )
    llm_allow_insecure_http: bool = field(
        default_factory=lambda: _bool_env("INTERVIEW_LLM_ALLOW_INSECURE_HTTP", False)
    )
    retention_days: int = field(
        default_factory=lambda: int(os.getenv("INTERVIEW_RETENTION_DAYS", "90"))
    )
    max_retention_days: int = field(
        default_factory=lambda: int(os.getenv("INTERVIEW_MAX_RETENTION_DAYS", "180"))
    )
    require_recording_notice: bool = field(
        default_factory=lambda: _bool_env("INTERVIEW_REQUIRE_RECORDING_NOTICE", True)
    )
    recording_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("INTERVIEW_RECORDING_DIR", "./data/recordings")
        )
    )
    max_audio_chunk_bytes: int = field(
        default_factory=lambda: int(os.getenv("INTERVIEW_MAX_AUDIO_CHUNK_BYTES", "65536"))
    )
    audio_sample_rate: int = field(
        default_factory=lambda: int(os.getenv("INTERVIEW_AUDIO_SAMPLE_RATE", "16000"))
    )
    audio_channels: int = field(
        default_factory=lambda: int(os.getenv("INTERVIEW_AUDIO_CHANNELS", "1"))
    )
    pipecat_enabled: bool = field(
        default_factory=lambda: _bool_env("INTERVIEW_PIPECAT_ENABLED", True)
    )
    asr_provider: str = field(
        default_factory=lambda: os.getenv("INTERVIEW_ASR_PROVIDER", "disabled")
    )
    asr_engine_model_type: str = field(
        default_factory=lambda: os.getenv("INTERVIEW_ASR_ENGINE_MODEL_TYPE", "16k_zh_en_speaker_2.0")
    )
    tencent_asr_app_id: str | None = field(
        default_factory=lambda: os.getenv("TENCENT_ASR_APP_ID") or None
    )
    tencent_asr_secret_id: str | None = field(
        default_factory=lambda: os.getenv("TENCENT_ASR_SECRET_ID") or None
    )
    tencent_asr_secret_key: str | None = field(
        default_factory=lambda: os.getenv("TENCENT_ASR_SECRET_KEY") or None
    )
    tencent_asr_hotwords: str | None = field(
        default_factory=lambda: os.getenv("TENCENT_ASR_HOTWORDS") or None
    )
    session_secret: str = field(
        default_factory=lambda: os.getenv("INTERVIEW_SESSION_SECRET", "development-only-change-me")
    )
    session_hours: int = field(
        default_factory=lambda: int(os.getenv("INTERVIEW_SESSION_HOURS", "12"))
    )
    feishu_app_id: str | None = field(
        default_factory=lambda: os.getenv("FEISHU_APP_ID") or None
    )
    feishu_app_secret: str | None = field(
        default_factory=lambda: os.getenv("FEISHU_APP_SECRET") or None
    )
    feishu_redirect_uri: str | None = field(
        default_factory=lambda: os.getenv("FEISHU_REDIRECT_URI") or None
    )
    feishu_oauth_scopes: str = field(
        default_factory=lambda: os.getenv("FEISHU_OAUTH_SCOPES", "auth:user.id:read")
    )
    feishu_notifications_enabled: bool = field(
        default_factory=lambda: _bool_env("FEISHU_NOTIFICATIONS_ENABLED", False)
    )
    public_base_url: str = field(
        default_factory=lambda: os.getenv("INTERVIEW_PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    )
    feishu_hr_open_ids: tuple[str, ...] = field(
        default_factory=lambda: tuple(filter(None, (value.strip() for value in os.getenv("FEISHU_HR_OPEN_IDS", "").split(","))))
    )
    feishu_admin_open_ids: tuple[str, ...] = field(
        default_factory=lambda: tuple(filter(None, (value.strip() for value in os.getenv("FEISHU_ADMIN_OPEN_IDS", "").split(","))))
    )
    knowledge_vault_dir: Path | None = field(
        default_factory=lambda: _optional_path_env("INTERVIEW_KNOWLEDGE_VAULT_DIR")
    )
    knowledge_vault_name: str = field(
        default_factory=lambda: os.getenv("INTERVIEW_KNOWLEDGE_VAULT_NAME", "Interview-Knowledge")
    )

    def validate(self) -> None:
        if not 1 <= self.retention_days <= self.max_retention_days <= 180:
            raise ValueError("Retention must be between 1 and 180 days")
        if self.provider_mode not in {"mock", "production"}:
            raise ValueError("INTERVIEW_PROVIDER_MODE must be mock or production")
        if not 3 <= self.llm_timeout_seconds <= 60:
            raise ValueError("INTERVIEW_LLM_TIMEOUT_SECONDS must be between 3 and 60")
        if not 2000 <= self.llm_max_context_chars <= 50000:
            raise ValueError("INTERVIEW_LLM_MAX_CONTEXT_CHARS must be between 2000 and 50000")
        parsed_llm_url = urlparse(self.llm_base_url)
        if parsed_llm_url.scheme not in {"http", "https"} or not parsed_llm_url.netloc:
            raise ValueError("INTERVIEW_LLM_BASE_URL must be an absolute HTTP(S) URL")
        if parsed_llm_url.username or parsed_llm_url.password or parsed_llm_url.query or parsed_llm_url.fragment:
            raise ValueError("INTERVIEW_LLM_BASE_URL cannot contain credentials, query, or fragment")
        if parsed_llm_url.scheme == "http" and not self.llm_allow_insecure_http:
            raise ValueError("HTTP LLM endpoints require INTERVIEW_LLM_ALLOW_INSECURE_HTTP=true")
        if self.audio_sample_rate != 16000 or self.audio_channels != 1:
            raise ValueError("The MVP audio protocol requires mono 16 kHz PCM")
        if not 1024 <= self.max_audio_chunk_bytes <= 1_048_576:
            raise ValueError("INTERVIEW_MAX_AUDIO_CHUNK_BYTES is outside the safe range")
        if self.asr_provider not in {"disabled", "tencent"}:
            raise ValueError("INTERVIEW_ASR_PROVIDER must be disabled or tencent")
        if self.environment == "production" and self.session_secret == "development-only-change-me":
            raise ValueError("INTERVIEW_SESSION_SECRET must be changed in production")
        if not 1 <= self.session_hours <= 168:
            raise ValueError("INTERVIEW_SESSION_HOURS must be between 1 and 168")
        if not self.knowledge_vault_name.strip():
            raise ValueError("INTERVIEW_KNOWLEDGE_VAULT_NAME cannot be empty")
        parsed_public_url = urlparse(self.public_base_url)
        if parsed_public_url.scheme not in {"http", "https"} or not parsed_public_url.netloc:
            raise ValueError("INTERVIEW_PUBLIC_BASE_URL must be an absolute HTTP(S) URL")

    @property
    def asr_configured(self) -> bool:
        if self.asr_provider == "disabled":
            return False
        if self.asr_provider == "tencent":
            return all(
                (
                    self.tencent_asr_app_id,
                    self.tencent_asr_secret_id,
                    self.tencent_asr_secret_key,
                )
            )
        return False

    @property
    def feishu_oauth_configured(self) -> bool:
        return all((self.feishu_app_id, self.feishu_app_secret, self.feishu_redirect_uri))

    @property
    def feishu_notifications_configured(self) -> bool:
        return bool(
            self.feishu_notifications_enabled
            and self.feishu_app_id
            and self.feishu_app_secret
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model and self.llm_base_url)

    @property
    def resolved_knowledge_vault_dir(self) -> Path | None:
        if self.knowledge_vault_dir:
            return self.knowledge_vault_dir
        if self.environment == "development":
            local_default = Path("D:/Interview-Knowledge")
            if local_default.is_dir():
                return local_default
        return None
