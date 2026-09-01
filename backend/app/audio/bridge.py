from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

try:
    from pipecat.frames.frames import InputAudioRawFrame
except ImportError:  # The voice extra is optional for API-only deployments.
    InputAudioRawFrame = None  # type: ignore[assignment,misc]


FrameConsumer = Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class AudioFrameInfo:
    byte_count: int
    sample_rate: int
    num_channels: int
    num_frames: int
    backend: str


def pipecat_available() -> bool:
    return InputAudioRawFrame is not None


class AudioFrameBridge:
    """Turns the browser PCM protocol into Pipecat input frames.

    An STT service can be attached later through ``consumer``. Until then, the
    bridge still validates the frame contract and reports truthful capability
    state without pretending that transcription occurred.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        consumer: FrameConsumer | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.consumer = consumer
        self.backend = "pipecat-input-frame" if pipecat_available() else "native-frame-bridge"

    async def push(self, pcm_s16le: bytes) -> AudioFrameInfo:
        num_frames = len(pcm_s16le) // (2 * self.channels)
        if InputAudioRawFrame is not None:
            frame: Any = InputAudioRawFrame(
                audio=pcm_s16le,
                sample_rate=self.sample_rate,
                num_channels=self.channels,
            )
        else:
            frame = {
                "audio": pcm_s16le,
                "sample_rate": self.sample_rate,
                "num_channels": self.channels,
                "num_frames": num_frames,
            }
        if self.consumer is not None:
            await self.consumer(frame)
        return AudioFrameInfo(
            byte_count=len(pcm_s16le),
            sample_rate=self.sample_rate,
            num_channels=self.channels,
            num_frames=num_frames,
            backend=self.backend,
        )

