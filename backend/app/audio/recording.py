from __future__ import annotations

import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from sys import byteorder


class InvalidAudioChunk(ValueError):
    pass


@dataclass
class AudioRecordingSession:
    path: Path
    sample_rate: int = 16000
    channels: int = 1
    sample_width_bytes: int = 2
    max_chunk_bytes: int = 65536
    byte_count: int = 0
    chunk_count: int = 0
    peak_level: float = 0

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(self.channels)
        self._wav.setsampwidth(self.sample_width_bytes)
        self._wav.setframerate(self.sample_rate)
        self._closed = False

    @property
    def duration_ms(self) -> int:
        bytes_per_second = self.sample_rate * self.channels * self.sample_width_bytes
        return round(self.byte_count / bytes_per_second * 1000)

    def append(self, pcm_s16le: bytes) -> dict[str, float | int]:
        if self._closed:
            raise InvalidAudioChunk("recording session is closed")
        if not pcm_s16le:
            raise InvalidAudioChunk("audio chunk is empty")
        if len(pcm_s16le) > self.max_chunk_bytes:
            raise InvalidAudioChunk("audio chunk exceeds the configured limit")
        frame_size = self.channels * self.sample_width_bytes
        if len(pcm_s16le) % frame_size:
            raise InvalidAudioChunk("audio chunk is not aligned to PCM frame size")

        self._wav.writeframesraw(pcm_s16le)
        self.byte_count += len(pcm_s16le)
        self.chunk_count += 1
        samples = array("h")
        samples.frombytes(pcm_s16le)
        if byteorder != "little":
            samples.byteswap()
        peak = max((abs(sample) for sample in samples), default=0) / 32767
        self.peak_level = max(self.peak_level, min(1.0, peak))
        return {
            "byte_count": self.byte_count,
            "chunk_count": self.chunk_count,
            "duration_ms": self.duration_ms,
            "chunk_peak": round(min(1.0, peak), 4),
            "peak_level": round(self.peak_level, 4),
        }

    def close(self) -> None:
        if not self._closed:
            self._wav.close()
            self._closed = True

    def __enter__(self) -> "AudioRecordingSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
