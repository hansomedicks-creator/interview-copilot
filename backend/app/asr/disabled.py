from __future__ import annotations


class DisabledASRSession:
    name = "disabled"
    configured = False

    async def start(self) -> None:
        return

    async def push_audio(self, pcm_s16le: bytes) -> None:
        return

    async def finish(self) -> None:
        return

    async def close(self) -> None:
        return

