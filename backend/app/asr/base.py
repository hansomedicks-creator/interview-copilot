from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class ASREvent:
    text: str
    is_final: bool
    start_ms: int
    end_ms: int
    provider: str
    utterance_index: int | None = None
    speaker_id: int | None = None
    confidence: float | None = None
    words: list[dict[str, Any]] = field(default_factory=list)


ASREventHandler = Callable[[ASREvent], Awaitable[None]]


class ASRProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class StreamingASRSession(Protocol):
    name: str
    configured: bool

    async def start(self) -> None: ...

    async def push_audio(self, pcm_s16le: bytes) -> None: ...

    async def finish(self) -> None: ...

    async def close(self) -> None: ...
