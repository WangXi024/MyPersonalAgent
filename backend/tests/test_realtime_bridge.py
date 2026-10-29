"""No paid providers: exercise the real ASGI socket with synthetic SDK events and PCM."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_ai.messages import PartStartEvent, PartDeltaEvent, PartEndEvent, SpeechPart, SpeechPartDelta
from pydantic_ai.messages import RealtimeInputSpeechStartEvent
import pytest
from starlette.websockets import WebSocketDisconnect

from app.api import voice_socket as api
from app.domain import voice as domain
from app.domain.common import DomainError
from app.integrations import realtime


class SyntheticSession:
    audio_input_sample_rate = 16000
    audio_output_sample_rate = 24000
    profile = {"supports_interruption": True, "supports_output_truncation": True}

    def __init__(self, events=()):
        self.events = list(events)
        self.closed = False
        self.received = []
        self.interrupts = []
        self.wait = asyncio.Event()

    async def __aiter__(self):
        for event in self.events:
            yield event
        await self.wait.wait()

    async def send_audio(self, data):
        self.received.append(data)

    async def commit_audio(self):
        pass

    async def interrupt(self, **kwargs):
        self.interrupts.append(kwargs)


@pytest.fixture
def socket_system(monkeypatch):
    scope = {"allowed": True, "remaining_seconds": 300, "idle_seconds": 60, "context": []}
    finished, captions, sessions, opens = [], [], [], []
    initial_events = []
    monkeypatch.setattr(api, "get_settings", lambda: NS(public_url="http://testserver"))
    monkeypatch.setattr(domain, "authenticate_login", AsyncMock(return_value={"user_id": "user", "auth_session_id": "login"}))
    monkeypatch.setattr(domain, "consume_ticket", AsyncMock(return_value=scope))
    monkeypatch.setattr(domain, "authorize_session", AsyncMock(return_value=scope))
    monkeypatch.setattr(domain, "mark_connected", AsyncMock(return_value=scope))
    monkeypatch.setattr(domain, "heartbeat", AsyncMock(return_value=scope))
    monkeypatch.setattr(domain, "load_credentials", AsyncMock(return_value={"api_key": SecretStr("never-expose-this-key")}))
    monkeypatch.setattr(domain, "record_usage", AsyncMock())

    async def caption(*args):
        captions.append(args[-1])

    async def finish(*args, **kwargs):
        finished.append(kwargs)

    monkeypatch.setattr(domain, "append_transcript", caption)
    monkeypatch.setattr(domain, "finish_session", finish)

    @asynccontextmanager
    async def factory(credentials, context):
        opens.append(True)
        session = SyntheticSession(initial_events)
        sessions.append(session)
        try:
            yield session, NS(requests=1, input_tokens=2, output_tokens=3, tool_calls=0)
        finally:
            session.closed = True

    monkeypatch.setattr(api, "voice_session", factory)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    with TestClient(app, raise_server_exceptions=True) as client:
        yield NS(client=client, scope=scope, events=initial_events, sessions=sessions,
                 finished=finished, captions=captions, opens=opens)


def connect(system, origin="http://testserver"):
    return system.client.websocket_connect("/api/v1/voice-sessions/session/socket",
        headers={"origin": origin, "cookie": "learning_session=valid"})


def authenticate(socket):
    socket.send_json({"type": "authenticate", "ticket": "single-use-ticket"})
    return socket.receive_json()


def receive_until(socket, kind):
    for _ in range(25):
        value = socket.receive_json()
        if value["type"] == kind:
            return value
        if value["type"] == "audio.chunk":
            socket.receive_bytes()
    raise AssertionError("expected event not received")


def audio_events():
    return [PartStartEvent(index=0, part=SpeechPart(speaker="assistant", transcript="")),
            PartDeltaEvent(index=0, delta=SpeechPartDelta(speaker="assistant", transcript="hello",
                                                        audio_chunk=b"\x00\x00" * 2400))]


def test_ready_uses_sdk_rates_binary_pcm_and_true_playback_ack(socket_system):
    system = socket_system
    system.events.extend(audio_events())
    with connect(system) as socket:
        ready = authenticate(socket)
        assert ready["type"] == "session.ready"
        assert (ready["input_sample_rate"], ready["output_sample_rate"]) == (16000, 24000)
        header = receive_until(socket, "audio.chunk")
        assert socket.receive_bytes() == b"\x00\x00" * 2400
        socket.send_bytes(b"\x00\x00" * 1600)
        socket.send_json({"type": "playback.ack", "response_id": header["response_id"], "played_bytes": 480})
        socket.send_json({"type": "interrupt", "response_id": header["response_id"], "played_bytes": 480})
        receive_until(socket, "session.state")
        socket.send_json({"type": "stop"})
        assert receive_until(socket, "session.ended")["reason"] == "stopped"
    assert system.sessions[0].interrupts == [{"played_ms": 10}]
    assert system.sessions[0].received == [b"\x00\x00" * 1600]
    assert system.sessions[0].closed
    assert system.finished[-1]["cleanup_confirmed"] is True
    assert system.captions[-1]["interrupted"] is True
    assert system.finished[-1]["usage"]["total_tokens"] == 5


def test_auto_barge_in_waits_for_playback_not_sent_bytes(socket_system):
    system = socket_system
    system.events.extend(audio_events() + [RealtimeInputSpeechStartEvent()])
    # The provider may raise speech-start before the writer got any audio. The
    # server correctly flushes unplayed queued PCM and asks for a real zero ACK.
    with connect(system) as socket:
        authenticate(socket)
        state = receive_until(socket, "session.state")
        assert state["state"] == "interrupted"
        socket.send_json({"type": "interrupt", "response_id": state["response_id"], "played_bytes": 0})
        receive_until(socket, "transcript")
        socket.send_json({"type": "stop"})
        receive_until(socket, "session.ended")
    assert system.sessions[0].interrupts == [{"played_ms": 0}]


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "", "https://testserver", "http://testserver.evil"])
def test_bad_origins_rejected_before_cookie_ticket_or_provider(socket_system, origin):
    with pytest.raises(WebSocketDisconnect):
        with connect(socket_system, origin):
            pass
    domain.authenticate_login.assert_not_awaited()
    domain.consume_ticket.assert_not_awaited()
    assert not socket_system.opens


def test_bad_cookie_never_opens_provider(socket_system):
    domain.authenticate_login.side_effect = DomainError("unauthenticated", "hidden", 401)
    with pytest.raises(WebSocketDisconnect):
        with connect(socket_system):
            pass
    assert not socket_system.opens


def test_invalid_or_replayed_ticket_never_constructs_provider(socket_system):
    domain.consume_ticket.side_effect = DomainError("voice_ticket_invalid", "hidden", 403)
    with connect(socket_system) as socket:
        socket.send_json({"type": "authenticate", "ticket": "already-used"})
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    domain.load_credentials.assert_not_awaited()
    assert not socket_system.opens


@pytest.mark.parametrize("frame", [b"\x01", b"\x00" * (api.MAX_FRAME + 2)], ids=["odd", "oversized"])
def test_invalid_pcm_size_ends_and_cleans_provider(socket_system, frame):
    with connect(socket_system) as socket:
        authenticate(socket)
        socket.send_bytes(frame)
        error = receive_until(socket, "session.error")
        assert error["code"] == "protocol_error"
        assert "key" not in str(error)
        receive_until(socket, "session.ended")
    assert socket_system.sessions[0].closed
    assert socket_system.finished[-1]["cleanup_confirmed"]


def test_forged_playback_beyond_offered_bytes_rejected(socket_system):
    socket_system.events.extend(audio_events())
    with connect(socket_system) as socket:
        authenticate(socket)
        header = receive_until(socket, "audio.chunk")
        socket.receive_bytes()
        socket.send_json({"type": "interrupt", "response_id": header["response_id"], "played_bytes": 999998})
        assert receive_until(socket, "session.error")["code"] == "protocol_error"
        receive_until(socket, "session.ended")
    assert not socket_system.sessions[0].interrupts
    assert socket_system.sessions[0].closed


def test_disconnect_closes_and_persists_partial_as_interrupted(socket_system):
    socket_system.events.append(PartStartEvent(index=1, part=SpeechPart(speaker="user", transcript="partial")))
    with connect(socket_system) as socket:
        authenticate(socket)
        receive_until(socket, "transcript")
    assert socket_system.sessions[0].closed
    assert socket_system.finished[-1]["reason"] == "disconnected"
    assert socket_system.captions == [{"index": 1, "speaker": "user", "text": "partial", "final": True, "interrupted": True}]


def test_final_transcript_saved_without_any_audio_retention(socket_system):
    socket_system.events.extend([PartStartEvent(index=2, part=SpeechPart(speaker="user", transcript="draft")),
        PartEndEvent(index=2, part=SpeechPart(speaker="user", transcript="final words"))])
    with connect(socket_system) as socket:
        authenticate(socket)
        receive_until(socket, "transcript")
        final = receive_until(socket, "transcript")
        assert final["final"]
        socket.send_json({"type": "stop"})
        receive_until(socket, "session.ended")
    assert socket_system.captions[-1]["text"] == "final words"
    assert all("audio" not in item for item in socket_system.captions)


def test_slow_handshake_renews_lease_before_connected(socket_system, monkeypatch):
    @asynccontextmanager
    async def slow(credentials, context):
        session = SyntheticSession()
        socket_system.sessions.append(session)
        try:
            await asyncio.sleep(1.2)
            assert domain.heartbeat.await_count >= 1
            yield session, NS()
        finally:
            session.closed = True

    monkeypatch.setattr(api, "voice_session", slow)
    with connect(socket_system) as socket:
        assert authenticate(socket)["type"] == "session.ready"
        socket.send_json({"type": "stop"})
        receive_until(socket, "session.ended")
    assert socket_system.sessions[0].closed
    assert socket_system.finished[-1]["cleanup_confirmed"] is True


def test_revoke_during_handshake_cancels_before_ready(socket_system, monkeypatch):
    @asynccontextmanager
    async def slow(credentials, context):
        session = SyntheticSession()
        socket_system.sessions.append(session)
        try:
            await asyncio.sleep(5)
            yield session, NS()
        finally:
            session.closed = True

    domain.heartbeat.return_value = {"allowed": False, "reason": "configuration_changed"}
    monkeypatch.setattr(api, "voice_session", slow)
    with connect(socket_system) as socket:
        socket.send_json({"type": "authenticate", "ticket": "ticket"})
        assert receive_until(socket, "session.error")["code"] == "configuration_changed"
        receive_until(socket, "session.ended")
    domain.mark_connected.assert_not_awaited()
    assert socket_system.sessions[0].closed
    assert socket_system.finished[-1]["cleanup_confirmed"] is True


@pytest.mark.parametrize("reason", ["permission_revoked", "configuration_changed", "lease_expired"])
def test_scope_rechecked_during_active_media(socket_system, reason):
    domain.heartbeat.return_value = {"allowed": False, "reason": reason}
    with connect(socket_system) as socket:
        authenticate(socket)
        assert receive_until(socket, "session.error")["code"] == reason
        receive_until(socket, "session.ended")
    assert socket_system.sessions[0].closed


@pytest.mark.parametrize("field,reason", [("idle_seconds", "idle_timeout"), ("remaining_seconds", "duration_limit")])
def test_silence_does_not_prevent_idle_or_duration_limits(socket_system, field, reason):
    socket_system.scope[field] = .1
    with connect(socket_system) as socket:
        authenticate(socket)
        socket.send_bytes(b"\x00\x00" * 1600)
        assert receive_until(socket, "session.error")["code"] == reason
        receive_until(socket, "session.ended")
    assert socket_system.sessions[0].closed


def test_audio_rate_burst_beyond_two_seconds_is_rejected(socket_system):
    with connect(socket_system) as socket:
        authenticate(socket)
        socket.send_bytes(b"\x00\x00" * 32000)
        socket.send_bytes(b"\x00\x00" * 32000)
        assert receive_until(socket, "session.error")["code"] == "protocol_error"
        receive_until(socket, "session.ended")
    assert socket_system.sessions[0].closed


def test_input_backpressure_cancels_slow_provider(socket_system, monkeypatch):
    async def slow_audio(self, data):
        await self.wait.wait()

    monkeypatch.setattr(SyntheticSession, "send_audio", slow_audio)
    with connect(socket_system) as socket:
        authenticate(socket)
        for _ in range(25):
            socket.send_bytes(b"\x00\x00" * 160)
        assert receive_until(socket, "session.error")["code"] == "backpressure"
        receive_until(socket, "session.ended")
    assert socket_system.sessions[0].closed


def test_fast_sdk_burst_over_queue_size_is_sent_without_false_backpressure(socket_system):
    socket_system.events.append(PartStartEvent(index=0, part=SpeechPart(speaker="assistant", transcript="")))
    socket_system.events.extend(PartDeltaEvent(index=0, delta=SpeechPartDelta(speaker="assistant",
        audio_chunk=b"\x00\x00" * 320)) for _ in range(100))
    with connect(socket_system) as socket:
        authenticate(socket)
        received = 0
        for _ in range(100):
            header = receive_until(socket, "audio.chunk")
            chunk = socket.receive_bytes()
            assert len(chunk) == header["bytes"]
            received += len(chunk)
        assert received == 64000
        socket.send_json({"type": "stop"})
        assert receive_until(socket, "session.ended")["reason"] == "stopped"
    assert socket_system.sessions[0].closed


def test_real_slow_sender_remains_bounded_and_stops_with_backpressure(socket_system, monkeypatch):
    original = api.WebSocket.send_json
    async def stalled(self, value, *args, **kwargs):
        if value.get("type") == "audio.chunk":
            await asyncio.Event().wait()
        return await original(self, value, *args, **kwargs)
    monkeypatch.setattr(api.WebSocket, "send_json", stalled)
    socket_system.events.append(PartStartEvent(index=0, part=SpeechPart(speaker="assistant", transcript="")))
    socket_system.events.extend(PartDeltaEvent(index=0, delta=SpeechPartDelta(speaker="assistant",
        audio_chunk=b"\x00\x00" * 320)) for _ in range(100))
    with connect(socket_system) as socket:
        authenticate(socket)
        assert receive_until(socket, "session.error")["code"] == "backpressure"
        receive_until(socket, "session.ended")
    assert socket_system.sessions[0].closed
    assert socket_system.finished[-1]["cleanup_confirmed"]


def test_sdk_exception_text_and_keys_never_reach_events_or_logs(socket_system, monkeypatch, caplog):
    @asynccontextmanager
    async def failed(*args):
        raise RuntimeError("never-expose-this-key and private transcript and PCM")
        yield

    monkeypatch.setattr(api, "voice_session", failed)
    with connect(socket_system) as socket:
        socket.send_json({"type": "authenticate", "ticket": "ticket"})
        error = receive_until(socket, "session.error")
        assert error["code"] == "provider_error"
        assert "never-expose" not in str(error)
        receive_until(socket, "session.ended")
    assert "never-expose" not in caplog.text


def test_real_tcp_websocket_pcm_without_paid_provider(socket_system):
    import json
    import socket as sockets
    import threading
    import time
    import uvicorn
    from websockets.sync.client import connect as ws_connect

    socket_system.events.extend(audio_events())
    listener = sockets.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(socket_system.client.app, lifespan="off", log_level="critical",
        access_log=False, ws_max_size=65536, ws_max_queue=16))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        with ws_connect(f"ws://127.0.0.1:{port}/api/v1/voice-sessions/session/socket", proxy=None,
                        origin="http://testserver", additional_headers={"Cookie": "learning_session=valid"}) as socket:
            socket.send(json.dumps({"type": "authenticate", "ticket": "tcp-fixture-ticket"}))
            assert json.loads(socket.recv(timeout=3))["type"] == "session.ready"
            for _ in range(10):
                header = json.loads(socket.recv(timeout=3))
                if header["type"] == "audio.chunk":
                    break
            assert header["type"] == "audio.chunk"
            assert socket.recv(timeout=3) == b"\x00\x00" * 2400
            socket.send(b"\x00\x00" * 1600)
            socket.send(json.dumps({"type": "interrupt", "response_id": header["response_id"], "played_bytes": 480}))
            for _ in range(10):
                if json.loads(socket.recv(timeout=3))["type"] == "session.state":
                    break
            socket.send(json.dumps({"type": "stop"}))
            for _ in range(10):
                if json.loads(socket.recv(timeout=3))["type"] == "session.ended":
                    break
        assert socket_system.sessions[0].closed
        assert socket_system.sessions[0].interrupts == [{"played_ms": 10}]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()


def test_local_readiness_does_not_construct_any_provider(monkeypatch):
    monkeypatch.setattr(realtime, "find_spec", lambda name: object() if name in {"openai", "websockets"} else None)
    assert realtime.provider_readiness(None)["dependency_ready"] is False
    assert realtime.provider_readiness("openai")["dependency_ready"] is True
    assert realtime.provider_readiness("azure")["dependency_ready"] is True
    assert realtime.provider_readiness("google")["dependency_ready"] is False
    assert realtime.provider_readiness("xai")["dependency_ready"] is False
    assert len(realtime.public_provider_options()) == 4


@pytest.mark.asyncio
async def test_native_openai_explicit_credentials_and_official_url_without_connect(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-use-env-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://must-not-use.invalid")
    monkeypatch.setenv("OPENAI_ORG_ID", "must-not-use-env-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "must-not-use-env-project")
    async with realtime.native_model({"provider": "openai", "model_id": "gpt-realtime", "voice": "alloy",
                                     "api_key": SecretStr("explicit-fixture-key")}) as model:
        assert type(model).__name__ == "OpenAIRealtimeModel"
        assert model.client.api_key == "explicit-fixture-key"
        assert str(model.client.base_url) == "https://api.openai.com/v1/"
        assert model.client.organization == model.client.project == ""


@pytest.mark.asyncio
async def test_native_azure_force_ga_and_no_environment_credentials(monkeypatch):
    monkeypatch.setenv("AZURE_VOICELIVE_ENDPOINT", "https://wrong.example")
    monkeypatch.setenv("AZURE_VOICELIVE_API_KEY", "must-not-use-env-key")
    monkeypatch.setenv("OPENAI_API_VERSION", "must-not-use-env-version")
    async with realtime.native_model({"provider": "azure", "model_id": "my-realtime", "voice": "alloy",
        "endpoint": "https://voice-fixture.openai.azure.com", "api_version": "2026-01-01",
        "api_key": SecretStr("explicit-fixture-key")}) as model:
        assert type(model).__name__ == "AzureRealtimeModel"
        assert model.client.api_key == "explicit-fixture-key"
        assert model.profile["azure_realtime_apis"] == ["azure_openai"]
        assert model._azure_provider.voice_live_endpoint == "https://voice-fixture.openai.azure.com"
        assert model._azure_provider.voice_live_api_key == "explicit-fixture-key"
        assert "wrong.example" not in model._realtime_url()


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [
    {"provider": None},
    {"provider": "google", "model_id": "gemini-live", "api_key": SecretStr("fixture")},
    {"provider": "openai", "model_id": "gpt-realtime", "api_key": SecretStr("fixture"), "base_url": "http://localhost:1"},
    {"provider": "openai", "model_id": "openai:chat-default", "api_key": SecretStr("fixture")},
])
async def test_unconfigured_missing_dependency_or_arbitrary_chat_endpoint_rejected(values):
    with pytest.raises(realtime.RealtimeUnavailable):
        async with realtime.native_model(values):
            raise AssertionError("must not open")
