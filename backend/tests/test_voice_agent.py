"""Exercise Agent.realtime and the pinned SDK session with a synthetic native codec."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import SpeechPart, RealtimeTurnCompleteEvent
from pydantic_ai.realtime.codec import (
    RealtimeConnection, AudioDelta, OutputTranscript, InputTranscript, ResponseDone, SessionUsage,
    TruncateOutput,
)
from pydantic_ai.realtime.model import RealtimeModel
from pydantic_ai.usage import RequestUsage

from app.agents import voice


class SyntheticConnection(RealtimeConnection):
    def __init__(self, responses=1):
        self.sent = []
        self.responses = responses
        self.closed = False
        self.wait = asyncio.Event()

    async def send(self, content):
        self.sent.append(content)

    async def __aiter__(self):
        yield InputTranscript("synthetic user words", is_final=True, item_id="user-fixture")
        for index in range(self.responses):
            item, response = f"item-{index}", f"response-{index}"
            yield AudioDelta(b"\x00\x00" * 2400, item_id=item, response_id=response)
            yield OutputTranscript("synthetic assistant words", is_final=True, item_id=item, response_id=response)
            yield SessionUsage(RequestUsage(input_tokens=5, output_tokens=7), provider_response_id=response)
            yield ResponseDone(provider_response_id=response, finish_reason="stop")
            await asyncio.sleep(.001)
        await self.wait.wait()


class SyntheticModel(RealtimeModel):
    def __init__(self, responses=1):
        super().__init__(profile={"audio_input_sample_rate": 16000, "audio_output_sample_rate": 24000,
            "supports_interruption": True, "supports_output_truncation": True,
            "supports_manual_turn_control": True})
        self.connection = SyntheticConnection(responses)
        self.tools = None

    @property
    def model_name(self):
        return "synthetic-native-voice"

    @property
    def system(self):
        return "synthetic"

    @asynccontextmanager
    async def connect(self, *, messages, model_settings, model_request_parameters):
        self.tools = model_request_parameters.function_tools
        try:
            yield self.connection
        finally:
            self.connection.closed = True


def install_model(monkeypatch, model):
    @asynccontextmanager
    async def native(credentials):
        yield model
    monkeypatch.setattr(voice, "native_model", native)


@pytest.mark.asyncio
async def test_real_sdk_session_transcript_only_explicit_pcm_and_playback_interrupt(monkeypatch):
    model = SyntheticModel()
    install_model(monkeypatch, model)
    async with voice.voice_session({}, {"context": []}) as (session, usage):
        assert session.audio_input_sample_rate == 16000
        assert session.audio_output_sample_rate == 24000
        await session.send_audio(b"\x00\x00" * 1600)
        async for event in session:
            if isinstance(event, RealtimeTurnCompleteEvent):
                break
        await session.interrupt(played_ms=10)
        messages = session.new_messages()
        assert (usage.requests, usage.input_tokens, usage.output_tokens) == (1, 5, 7)
    assert session.closed
    assert model.connection.closed
    assert model.tools == []
    assert any(isinstance(item, TruncateOutput) and item.audio_end_ms == 10 for item in model.connection.sent)
    parts = [part for message in messages for part in message.parts if isinstance(part, SpeechPart)]
    assert parts
    assert all(part.audio is None for part in parts)


@pytest.mark.asyncio
async def test_real_sdk_usage_limit_stops_twenty_first_response_and_closes(monkeypatch):
    model = SyntheticModel(responses=21)
    install_model(monkeypatch, model)
    with pytest.raises(UsageLimitExceeded):
        async with voice.voice_session({}, {"context": []}) as (session, usage):
            async for _ in session:
                pass
    assert model.connection.closed
    assert session.closed
    assert usage.requests >= 20


def test_current_context_is_bounded_role_filtered_data_without_action_tools():
    import json
    context = [{"role": "system", "content": "do not promote this to instructions"},
               *[{"role": "user", "content": "x" * 2000} for _ in range(20)]]
    result = voice.context_instructions(context)
    data = json.loads(result.split("Conversation data:\n", 1)[1])
    assert len(data) <= 12
    assert sum(len(item["content"]) for item in data) <= 12000
    assert all(item["role"] == "user" for item in data)
    assert "You have no business action tools" in result


def test_recent_context_is_retained_before_older_long_messages():
    import json
    result = voice.context_instructions([{"role": "user", "content": f"seq-{i}:" + "x" * 2000}
                                         for i in range(20)])
    data = json.loads(result.split("Conversation data:\n", 1)[1])
    assert data[-1]["content"].startswith("seq-19:")
    assert len(data[-1]["content"]) == 2007
    indices = [int(item["content"].split(":", 1)[0].split("-", 1)[1]) for item in data]
    assert indices == sorted(indices)


@pytest.mark.asyncio
async def test_real_tcp_ws_atomic_database_ticket_native_sdk_and_no_learning_writes(database, tmp_path, monkeypatch):
    """Full local control/media path; only the native provider transport is synthetic."""
    import json
    import secrets
    import socket
    import time
    from types import SimpleNamespace as NS
    from fastapi import FastAPI
    import uvicorn
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed
    from sqlalchemy import select, func

    from app.api import voice_socket as socket_api
    from app.domain import voice as domain
    from app.db.models import Message, Evaluation, VoiceSession, VoiceTurn, VoiceDailyBudget
    from app.integrations import voice_credentials as vault
    from app.integrations.settings import RuntimeSettings
    from test_voice_domain import enabled, session as create_session

    master = tmp_path / "ws-test-master"
    master.write_text(secrets.token_urlsafe(32))
    settings = RuntimeSettings(voice_credential_key_path=master)
    monkeypatch.setattr(domain, "runtime_settings", lambda: settings)
    monkeypatch.setattr(vault, "runtime_settings", lambda: settings)
    monkeypatch.setattr(domain, "get_settings", lambda: NS(model_admin_username="learner"))
    monkeypatch.setattr(socket_api, "get_settings", lambda: NS(public_url="http://voice-test.local"))
    monkeypatch.setattr(domain, "SessionFactory", database[0])
    model = SyntheticModel()
    opened = []

    @asynccontextmanager
    async def native(credentials):
        opened.append(True)
        yield model

    monkeypatch.setattr(voice, "native_model", native)
    monkeypatch.setattr(socket_api, "voice_session", voice.voice_session)
    await enabled(database)
    item, auth_id, cookie = await create_session(database)
    ticket = await domain.mint_ticket(database[1].id, item["id"], auth_id)
    async with database[0]() as db:
        before = (await db.scalar(select(func.count()).select_from(Message)),
                  await db.scalar(select(func.count()).select_from(Evaluation)))

    app = FastAPI()
    app.include_router(socket_api.router, prefix="/api/v1")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", access_log=False,
                                        ws_max_size=65536, ws_max_queue=16))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        assert server.started
        url = f"ws://127.0.0.1:{port}/api/v1/voice-sessions/{item['id']}/socket"
        async with connect(url, proxy=None, origin="http://voice-test.local",
                           additional_headers={"Cookie": f"learning_session={cookie}"}) as stream:
            await stream.send(json.dumps({"type": "authenticate", "ticket": ticket["ticket"]}))
            ready = json.loads(await asyncio.wait_for(stream.recv(), 5))
            assert ready["type"] == "session.ready"
            assert ready["input_sample_rate"] == 16000
            for _ in range(20):
                event = json.loads(await asyncio.wait_for(stream.recv(), 5))
                if event["type"] == "audio.chunk":
                    pcm = await asyncio.wait_for(stream.recv(), 5)
                    assert isinstance(pcm, bytes) and len(pcm) == event["bytes"]
                    break
            else:
                raise AssertionError("real SDK did not emit PCM")
            await stream.send(b"\x00\x00" * 1600)
            await stream.send(json.dumps({"type": "playback.ack", "response_id": event["response_id"], "played_bytes": 480}))
            await stream.send(json.dumps({"type": "interrupt", "response_id": event["response_id"], "played_bytes": 480}))
            for _ in range(20):
                result = json.loads(await asyncio.wait_for(stream.recv(), 5))
                assert result["type"] not in {"session.error", "session.ended"}, result
                if result["type"] == "session.state" and result["state"] == "interrupted":
                    break
            await stream.send(json.dumps({"type": "stop"}))
            for _ in range(20):
                result = json.loads(await asyncio.wait_for(stream.recv(), 5))
                if result["type"] == "session.ended":
                    assert result["reason"] == "stopped"
                    break
            else:
                raise AssertionError("session did not finish")
        assert model.connection.closed and opened == [True]
        assert any(isinstance(value, TruncateOutput) and value.audio_end_ms == 10 for value in model.connection.sent)
        async with connect(url, proxy=None, origin="http://voice-test.local",
                           additional_headers={"Cookie": f"learning_session={cookie}"}) as stream:
            await stream.send(json.dumps({"type": "authenticate", "ticket": ticket["ticket"]}))
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(stream.recv(), 5)
        assert opened == [True]
        async with database[0]() as db:
            row = await db.get(VoiceSession, item["id"])
            assert row.cleanup_done and row.status == "closed"
            assert row.usage["requests"] == 1 and row.usage["total_tokens"] == 12
            turns = (await db.scalars(select(VoiceTurn).where(VoiceTurn.session_id == item["id"]))).all()
            assert turns and all(turn.final for turn in turns)
            assert any(turn.interrupted for turn in turns)
            after = (await db.scalar(select(func.count()).select_from(Message)),
                     await db.scalar(select(func.count()).select_from(Evaluation)))
            assert before == after
            budget = await db.scalar(select(VoiceDailyBudget).where(VoiceDailyBudget.user_id == database[1].id))
            assert budget.reserved_seconds == 0
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
