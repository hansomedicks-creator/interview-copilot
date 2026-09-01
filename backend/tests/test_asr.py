from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient

from app.asr import ASREvent, ASRProviderError
from app.asr.factory import asr_capability
from app.asr.tencent import TencentRealtimeASRSession
from app.config import Settings
from app.main import create_app


async def discard(_: ASREvent) -> None:
    return


def test_tencent_signed_url_uses_documented_hmac_shape():
    session = TencentRealtimeASRSession(
        app_id="1250000000",
        secret_id="secret-id",
        secret_key="secret-key",
        engine_model_type="16k_zh_en",
        event_handler=discard,
        clock=lambda: 1_700_000_000,
        nonce_factory=lambda: 123456,
        voice_id_factory=lambda: "voice-fixed",
    )

    signed = urlsplit(session.signed_url())
    query = parse_qs(signed.query)
    signature = query.pop("signature")[0]
    unsigned_query = urlencode(sorted((key, value[0]) for key, value in query.items()))
    source = f"asr.cloud.tencent.com/asr/v2/1250000000?{unsigned_query}"
    expected = base64.b64encode(
        hmac.new(b"secret-key", source.encode(), hashlib.sha1).digest()
    ).decode()

    assert signed.scheme == "wss"
    assert query["engine_model_type"] == ["16k_zh_en"]
    assert query["voice_format"] == ["1"]
    assert signature == expected


class PacketSocket:
    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class ReceiveSocket(PacketSocket):
    def __init__(self, messages: list[dict]) -> None:
        super().__init__()
        self.messages = [json.dumps(item) for item in messages]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


def test_tencent_packetizes_16k_pcm_into_200ms_frames():
    async def run():
        session = TencentRealtimeASRSession(
            app_id="1",
            secret_id="id",
            secret_key="key",
            engine_model_type="16k_zh_en",
            event_handler=discard,
        )
        socket = PacketSocket()
        session._socket = socket
        session._started = True
        await session.push_audio(b"a" * 3200)
        assert socket.sent == []
        await session.push_audio(b"b" * 4000)
        assert socket.sent == [b"a" * 3200 + b"b" * 3200]
        assert session._audio_buffer == b"b" * 800

    asyncio.run(run())


def test_tencent_paces_backlogged_audio_at_real_time_rate():
    async def run():
        now = 10.0
        waits: list[float] = []

        async def advance(seconds: float) -> None:
            nonlocal now
            waits.append(seconds)
            now += seconds

        session = TencentRealtimeASRSession(
            app_id="1",
            secret_id="id",
            secret_key="key",
            engine_model_type="16k_zh_en",
            event_handler=discard,
            monotonic=lambda: now,
            sleeper=advance,
        )
        socket = PacketSocket()
        session._socket = socket
        session._started = True

        await session.push_audio(b"a" * 12_800)

        assert socket.sent == [b"a" * 6_400, b"a" * 6_400]
        assert len(waits) == 1
        assert abs(waits[0] - 0.2) < 1e-9

    asyncio.run(run())


def test_tencent_receive_loop_is_not_poisoned_by_downstream_handler_failure():
    async def run():
        handled = asyncio.Event()

        async def failing_handler(_: ASREvent) -> None:
            handled.set()
            raise RuntimeError("local transcript persistence failed")

        session = TencentRealtimeASRSession(
            app_id="1",
            secret_id="id",
            secret_key="key",
            engine_model_type="16k_zh_en",
            event_handler=failing_handler,
        )
        session._socket = ReceiveSocket(
            [
                {
                    "result": {
                        "voice_text_str": "快速回答中的稳定句子",
                        "slice_type": 2,
                        "start_time": 0,
                        "end_time": 500,
                    },
                    "final": 1,
                }
            ]
        )
        session._event_queue = asyncio.Queue(maxsize=8)
        session._event_task = asyncio.create_task(session._dispatch_events())
        session._started = True
        session._receiver_task = asyncio.create_task(session._receive_loop())

        await asyncio.wait_for(session._provider_final.wait(), timeout=1)
        await asyncio.wait_for(handled.wait(), timeout=1)
        await asyncio.wait_for(session._event_queue.join(), timeout=1)
        assert session._receiver_error is None
        await session.close()

    asyncio.run(run())


def test_tencent_result_distinguishes_interim_and_final():
    session = TencentRealtimeASRSession(
        app_id="1",
        secret_id="id",
        secret_key="key",
        engine_model_type="16k_zh_en",
        event_handler=discard,
    )
    interim = session.parse_result(
        {"voice_text_str": "候选人正在回答", "slice_type": 1, "start_time": 10, "end_time": 90}
    )
    final = session.parse_result(
        {"voice_text_str": "候选人回答完成", "slice_type": 2, "start_time": 10, "end_time": 120}
    )
    assert interim.is_final is False
    assert final.is_final is True
    assert final.start_ms == 10
    assert final.end_ms == 120


def test_tencent_v2_result_exposes_speaker_ids_and_deduplicates_sentences():
    session = TencentRealtimeASRSession(
        app_id="1",
        secret_id="id",
        secret_key="key",
        engine_model_type="16k_zh_en_speaker_2.0",
        event_handler=discard,
    )
    payload = {
        "sentences": {
            "sentence_list": [
                {
                    "sentence_id": 3,
                    "sentence_type": 1,
                    "speaker_id": 1,
                    "sentence": "我负责上一份工作的增长项目。",
                    "start_time": 120,
                    "end_time": 860,
                }
            ]
        }
    }
    events = session.parse_payload(payload)
    assert len(events) == 1
    assert events[0].is_final is True
    assert events[0].speaker_id == 1
    assert events[0].utterance_index == 3
    assert session.parse_payload(payload) == []


def test_capability_never_exposes_credentials(tmp_path):
    settings = Settings(
        recording_dir=tmp_path,
        asr_provider="tencent",
        tencent_asr_app_id="appid",
        tencent_asr_secret_id="secretid",
        tencent_asr_secret_key="secretkey",
    )
    capability = asr_capability(settings)
    serialized = json.dumps(capability)
    assert capability["status"] == "ready"
    assert "secretid" not in serialized
    assert "secretkey" not in serialized


class FakeASRSession:
    name = "fake"
    configured = True

    def __init__(self, handler):
        self.handler = handler
        self.emitted = False

    async def start(self):
        return

    async def push_audio(self, pcm_s16le: bytes):
        if self.emitted:
            return
        self.emitted = True
        await self.handler(ASREvent("候选人正在回答", False, 0, 80, self.name))
        await self.handler(ASREvent("候选人回答完成", True, 0, 100, self.name))

    async def finish(self):
        return

    async def close(self):
        return


class RecoveringFakeASRSession:
    name = "recovering-fake"
    configured = True

    def __init__(self, handler, *, fail_push: bool):
        self.handler = handler
        self.fail_push = fail_push

    async def start(self):
        return

    async def push_audio(self, pcm_s16le: bytes):
        if self.fail_push:
            raise ASRProviderError("temporary disconnect", code="5000", retryable=True)

    async def finish(self):
        return

    async def close(self):
        return


def test_audio_socket_streams_and_persists_asr_results(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        settings=Settings(environment="test", recording_dir=tmp_path / "recordings"),
    )
    app.state.asr_session_factory = lambda handler: FakeASRSession(handler)

    with TestClient(app) as client:
        boot = client.post("/api/v1/demo/bootstrap").json()
        interview_id = boot["active_interview_id"]
        client.post(
            f"/api/v1/interviews/{interview_id}/notice",
            json={"acknowledged_by": "tester", "candidate_was_notified": True},
        )
        client.post(f"/api/v1/interviews/{interview_id}/start")

        with client.websocket_connect(f"/ws/interviews/{interview_id}/audio") as socket:
            socket.send_json(
                {
                    "type": "audio.start",
                    "audio": {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                }
            )
            ready = socket.receive_json()
            assert ready["pipeline"]["asr_status"] == "ready"
            socket.send_bytes(b"\x00\x00" * 1600)
            interim = socket.receive_json()
            final = socket.receive_json()
            metrics = socket.receive_json()
            assert interim["type"] == "transcript.interim"
            assert final["type"] == "transcript.final"
            assert final["segment"]["text_raw"] == "候选人回答完成"
            assert final["segment"]["speaker_role"] == "unknown"
            assert "analysis" in final
            assert metrics["type"] == "audio.metrics"
            socket.send_json({"type": "audio.stop"})
            assert socket.receive_json()["type"] == "audio.stopped"

        segments = client.get(f"/api/v1/interviews/{interview_id}/segments").json()
        assert [segment["text_raw"] for segment in segments] == ["候选人回答完成"]


def test_audio_socket_recovers_once_from_retryable_asr_disconnect(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'recover.db'}",
        settings=Settings(environment="test", recording_dir=tmp_path / "recordings"),
    )
    created = 0

    def session_factory(handler):
        nonlocal created
        created += 1
        return RecoveringFakeASRSession(handler, fail_push=created == 1)

    app.state.asr_session_factory = session_factory

    with TestClient(app) as client:
        boot = client.post("/api/v1/demo/bootstrap").json()
        interview_id = boot["active_interview_id"]
        client.post(
            f"/api/v1/interviews/{interview_id}/notice",
            json={"acknowledged_by": "tester", "candidate_was_notified": True},
        )
        client.post(f"/api/v1/interviews/{interview_id}/start")

        with client.websocket_connect(f"/ws/interviews/{interview_id}/audio") as socket:
            socket.send_json(
                {
                    "type": "audio.start",
                    "audio": {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                }
            )
            assert socket.receive_json()["type"] == "audio.ready"
            socket.send_bytes(b"\x00\x00" * 1600)
            reconnecting = socket.receive_json()
            assert reconnecting["type"] == "asr.status"
            assert reconnecting["status"] == "recovering"
            recovered = socket.receive_json()
            assert recovered["type"] == "asr.status"
            assert recovered["status"] == "ready"
            assert recovered["recovered"] is True
            assert socket.receive_json()["type"] == "audio.metrics"
            assert created == 2
            socket.send_json({"type": "audio.stop"})
            assert socket.receive_json()["type"] == "audio.stopped"


class FakeSpeakerASRSession:
    name = "fake-speaker"
    configured = True

    def __init__(self, handler):
        self.handler = handler
        self.emitted = False

    async def start(self):
        return

    async def push_audio(self, pcm_s16le: bytes):
        if self.emitted:
            return
        self.emitted = True
        await self.handler(
            ASREvent(
                "请你介绍一下上一份工作中负责的项目？",
                True,
                0,
                800,
                self.name,
                speaker_id=0,
            )
        )
        await self.handler(
            ASREvent(
                "我负责上一份工作的增长项目，最终把转化率提升了百分之十。",
                True,
                900,
                2200,
                self.name,
                speaker_id=1,
            )
        )

    async def finish(self):
        return

    async def close(self):
        return


def test_speaker_diarization_auto_maps_roles_and_human_correction_relabels_history(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'speaker.db'}",
        settings=Settings(environment="test", recording_dir=tmp_path / "recordings"),
    )
    app.state.asr_session_factory = lambda handler: FakeSpeakerASRSession(handler)

    with TestClient(app) as client:
        boot = client.post("/api/v1/demo/bootstrap").json()
        interview_id = boot["active_interview_id"]
        client.post(
            f"/api/v1/interviews/{interview_id}/notice",
            json={"acknowledged_by": "tester", "candidate_was_notified": True},
        )
        client.post(f"/api/v1/interviews/{interview_id}/start")

        with client.websocket_connect(f"/ws/interviews/{interview_id}/audio") as socket:
            socket.send_json(
                {
                    "type": "audio.start",
                    "audio": {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                }
            )
            assert socket.receive_json()["type"] == "audio.ready"
            socket.send_bytes(b"\x00\x00" * 1600)
            first = socket.receive_json()
            second = socket.receive_json()
            assert first["segment"]["speaker_role"] == "interviewer"
            assert second["segment"]["speaker_role"] == "candidate"
            assert second["speaker_mappings"][1]["confidence"] >= 0.72
            next_message = socket.receive_json()
            if next_message["type"] == "analysis.update":
                next_message = socket.receive_json()
            assert next_message["type"] == "audio.metrics"
            socket.send_json({"type": "audio.stop"})
            assert socket.receive_json()["type"] == "audio.stopped"

        mappings = client.get(
            f"/api/v1/interviews/{interview_id}/speaker-mappings"
        ).json()
        assert [item["speaker_role"] for item in mappings] == ["interviewer", "candidate"]

        corrected = client.put(
            f"/api/v1/interviews/{interview_id}/speaker-mappings/0",
            json={"speaker_role": "candidate"},
        )
        assert corrected.status_code == 200
        assert [item["speaker_role"] for item in corrected.json()] == ["candidate", "interviewer"]
        segments = client.get(f"/api/v1/interviews/{interview_id}/segments").json()
        assert [item["speaker_role"] for item in segments] == ["candidate", "interviewer"]
