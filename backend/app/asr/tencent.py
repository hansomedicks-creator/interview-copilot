from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import random
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode
from uuid import uuid4

from websockets.asyncio.client import connect

from .base import ASREvent, ASREventHandler, ASRProviderError


class TencentRealtimeASRSession:
    name = "tencent"
    configured = True
    host = "asr.cloud.tencent.com"
    packet_bytes = 6400  # 200 ms of 16 kHz, 16-bit, mono PCM.

    def __init__(
        self,
        *,
        app_id: str,
        secret_id: str,
        secret_key: str,
        engine_model_type: str,
        event_handler: ASREventHandler,
        hotwords: str | None = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], int] | None = None,
        voice_id_factory: Callable[[], str] | None = None,
        connector=connect,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.app_id = app_id
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.engine_model_type = engine_model_type
        self.event_handler = event_handler
        self.hotwords = hotwords
        self.clock = clock
        self.nonce_factory = nonce_factory or (lambda: random.SystemRandom().randint(1, 2_147_483_647))
        self.voice_id_factory = voice_id_factory or (lambda: uuid4().hex)
        self.connector = connector
        self.monotonic = monotonic or (lambda: asyncio.get_running_loop().time())
        self.sleeper = sleeper
        self.voice_id = self.voice_id_factory()
        self._socket = None
        self._receiver_task: asyncio.Task | None = None
        self._event_task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue[ASREvent] | None = None
        self._audio_buffer = bytearray()
        self._started = False
        self._provider_final = asyncio.Event()
        self._receiver_error: ASRProviderError | None = None
        self._sentence_states: dict[int, tuple[str, bool, int | None]] = {}
        self._send_lock = asyncio.Lock()
        self._next_packet_at: float | None = None

    def signed_url(self) -> str:
        timestamp = int(self.clock())
        params: dict[str, str | int] = {
            "engine_model_type": self.engine_model_type,
            "expired": timestamp + 86400,
            "filter_dirty": 0,
            "filter_empty_result": 1,
            "filter_modal": 0,
            "filter_punc": 0,
            "needvad": 1,
            "nonce": self.nonce_factory(),
            "secretid": self.secret_id,
            "timestamp": timestamp,
            "voice_format": 1,
            "voice_id": self.voice_id,
        }
        if self.hotwords:
            params["hotword_list"] = self.hotwords
        query = urlencode(sorted(params.items()))
        sign_source = f"{self.host}/asr/v2/{self.app_id}?{query}"
        signature = base64.b64encode(
            hmac.new(self.secret_key.encode(), sign_source.encode(), hashlib.sha1).digest()
        ).decode()
        return f"wss://{sign_source}&signature={urlencode({'signature': signature})[10:]}"

    async def start(self) -> None:
        if self._started:
            return
        self._socket = await self.connector(
            self.signed_url(),
            open_timeout=10,
            close_timeout=3,
            max_size=2_000_000,
        )
        handshake = json.loads(await asyncio.wait_for(self._socket.recv(), timeout=10))
        self._assert_success(handshake)
        self._event_queue = asyncio.Queue(maxsize=128)
        self._event_task = asyncio.create_task(self._dispatch_events())
        self._next_packet_at = None
        self._started = True
        self._receiver_task = asyncio.create_task(self._receive_loop())

    async def push_audio(self, pcm_s16le: bytes) -> None:
        if self._receiver_error is not None:
            raise self._receiver_error
        if not self._started or self._socket is None:
            raise ASRProviderError("ASR session has not started", code="not_started")
        self._audio_buffer.extend(pcm_s16le)
        async with self._send_lock:
            while len(self._audio_buffer) >= self.packet_bytes:
                packet = bytes(self._audio_buffer[: self.packet_bytes])
                del self._audio_buffer[: self.packet_bytes]
                await self._send_audio_packet(packet)

    async def finish(self) -> None:
        if not self._started or self._socket is None:
            return
        async with self._send_lock:
            if self._audio_buffer:
                await self._send_audio_packet(bytes(self._audio_buffer))
                self._audio_buffer.clear()
        await self._socket.send(json.dumps({"type": "end"}))
        try:
            await asyncio.wait_for(self._provider_final.wait(), timeout=8)
        except TimeoutError:
            pass
        if self._event_queue is not None:
            try:
                await asyncio.wait_for(self._event_queue.join(), timeout=5)
            except TimeoutError:
                pass
        error = self._receiver_error
        await self.close()
        if error is not None:
            raise error

    async def close(self) -> None:
        if self._receiver_task:
            if not self._receiver_task.done():
                self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
            self._receiver_task = None
        if self._event_task:
            if not self._event_task.done():
                self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
            self._event_task = None
        self._event_queue = None
        if self._socket is not None:
            await self._socket.close()
        self._socket = None
        self._started = False
        self._next_packet_at = None

    async def _receive_loop(self) -> None:
        assert self._socket is not None
        try:
            async for raw in self._socket:
                payload = json.loads(raw)
                self._assert_success(payload)
                events = self.parse_payload(payload)
                for event in events:
                    await self._enqueue_event(event)
                if payload.get("final") == 1:
                    self._provider_final.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._provider_final.set()
            if isinstance(exc, ASRProviderError):
                self._receiver_error = exc
            else:
                self._receiver_error = ASRProviderError(str(exc), retryable=True)

    async def _enqueue_event(self, event: ASREvent) -> None:
        """Keep Tencent's receive loop independent from DB and LLM latency.

        Interim text is disposable under extreme pressure; final sentences are
        never intentionally dropped. This prevents downstream processing from
        being misreported as a Tencent connection failure.
        """
        if self._event_queue is None:
            return
        if not event.is_final:
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                return
            return
        await self._event_queue.put(event)

    async def _dispatch_events(self) -> None:
        assert self._event_queue is not None
        while True:
            event = await self._event_queue.get()
            try:
                await self.event_handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Transcript persistence or model analysis is an application
                # concern. It must not poison an otherwise healthy ASR socket.
                pass
            finally:
                self._event_queue.task_done()

    async def _send_audio_packet(self, packet: bytes) -> None:
        """Send 200 ms packets at a real-time 1:1 rate required by Tencent."""
        if self._socket is None:
            raise ASRProviderError("ASR socket is unavailable", code="not_started")
        now = self.monotonic()
        if self._next_packet_at is not None and self._next_packet_at > now:
            await self.sleeper(self._next_packet_at - now)
        await self._socket.send(packet)
        sent_at = self.monotonic()
        self._next_packet_at = sent_at + len(packet) / 32000

    def parse_result(self, result: dict) -> ASREvent:
        return ASREvent(
            text=result.get("voice_text_str", ""),
            is_final=result.get("slice_type") == 2,
            start_ms=int(result.get("start_time", 0)),
            end_ms=int(result.get("end_time", 0)),
            provider=self.name,
            utterance_index=result.get("index"),
            speaker_id=_optional_speaker_id(result.get("speaker_id")),
            words=result.get("word_list") or [],
        )

    def parse_payload(self, payload: dict) -> list[ASREvent]:
        sentences = payload.get("sentences")
        if sentences:
            return self._parse_v2_sentences(sentences)
        result = payload.get("result")
        if result and result.get("voice_text_str"):
            return [self.parse_result(result)]
        return []

    def _parse_v2_sentences(self, sentences: object) -> list[ASREvent]:
        if isinstance(sentences, list):
            items = sentences
        elif isinstance(sentences, dict):
            sentence_list = sentences.get("sentence_list")
            if isinstance(sentence_list, list):
                items = sentence_list
            elif sentences.get("sentence"):
                items = [sentences]
            else:
                items = []
        else:
            items = []
        events: list[ASREvent] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("sentence") or item.get("voice_text_str") or "").strip()
            if not text:
                continue
            sentence_id = int(item.get("sentence_id", item.get("index", 0)))
            is_final = int(item.get("sentence_type", item.get("slice_type", 0))) in {1, 2}
            speaker_id = _optional_speaker_id(item.get("speaker_id"))
            state = (text, is_final, speaker_id)
            if self._sentence_states.get(sentence_id) == state:
                continue
            previous = self._sentence_states.get(sentence_id)
            if previous and previous[1]:
                continue
            self._sentence_states[sentence_id] = state
            events.append(
                ASREvent(
                    text=text,
                    is_final=is_final,
                    start_ms=int(item.get("start_time", 0)),
                    end_ms=int(item.get("end_time", item.get("start_time", 0))),
                    provider=self.name,
                    utterance_index=sentence_id,
                    speaker_id=speaker_id,
                    words=item.get("word_list") or [],
                )
            )
        return events

    @staticmethod
    def _assert_success(payload: dict) -> None:
        code = int(payload.get("code", 0))
        if code:
            retryable = code in {4000, 4008, 4009, 5000, 5001, 5002}
            raise ASRProviderError(
                payload.get("message", "Tencent ASR error"),
                code=str(code),
                retryable=retryable,
            )


def _optional_speaker_id(value: object) -> int | None:
    try:
        speaker_id = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return speaker_id if 0 <= speaker_id <= 9 else None
