"""Bounded same-origin media bridge; one WebSocket reader, sender and SDK event consumer."""
import asyncio
from array import array
from contextlib import AsyncExitStack
import json
import sys
import time
from uuid import uuid4

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    PartStartEvent, PartDeltaEvent, PartEndEvent, SpeechPart, SpeechPartDelta,
    RealtimeInputSpeechStartEvent, RealtimeInputSpeechEndEvent, RealtimeTurnCompleteEvent,
    RealtimeResponseInterruptedEvent, RealtimeSessionErrorEvent, RealtimeSessionReconnectEvent,
)

from app.agents.voice import voice_session
from app.api.auth import COOKIE
from app.config import get_settings
from app.domain.common import DomainError

router = APIRouter()
MAX_FRAME = 65536
MAX_JSON = 4096
MAX_TRANSCRIPT = 8000
_LATE_CLEANUPS = set()


class BridgeStop(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _json(text):
    if not isinstance(text, str) or len(text.encode()) > MAX_JSON:
        raise BridgeStop("protocol_error")
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        raise BridgeStop("protocol_error") from None
    if not isinstance(result, dict) or not isinstance(result.get("type"), str):
        raise BridgeStop("protocol_error")
    return result


def _usage(usage, complete):
    if usage is None:
        return {"usage_complete": complete}
    return {**{name: max(0, int(getattr(usage, name, 0) or 0)) for name in
               ("requests", "input_tokens", "output_tokens", "tool_calls")},
            "total_tokens": max(0, int(getattr(usage, "input_tokens", 0) or 0)
                                + int(getattr(usage, "output_tokens", 0) or 0)),
            "audio_input_tokens": max(0, int(getattr(usage, "input_audio_tokens", 0) or 0)),
            "audio_output_tokens": max(0, int(getattr(usage, "output_audio_tokens", 0) or 0)),
            "usage_complete": complete}


class MediaBridge:
    def __init__(self, socket, domain, session_id, owner_id, auth_session_id, scope, credentials,
                 *, factory=voice_session, clock=time.monotonic):
        self.socket, self.domain, self.session_id = socket, domain, session_id
        self.owner_id, self.auth_session_id = owner_id, auth_session_id
        self.scope, self.credentials, self.factory = scope, credentials, factory
        self.clock = clock
        self.stop = asyncio.Event()
        self.input = asyncio.Queue(16)
        self.output = asyncio.Queue(32)
        self.domain_lock = asyncio.Lock()
        self.session = self.usage = None
        self.reason = "closed"
        self.cleanup_confirmed = False
        self.ready = False
        self.parts = {}
        self.current = None
        self.suppressed = set()
        self.pending_interrupt = None
        self.last_activity = self.started = self.clock()
        self.activity = False
        self.input_rate = self.output_rate = 0
        self.audio_credit = 0
        self.credit_at = self.clock()
        self.window_at, self.window_count = self.clock(), 0
        self.total_output = self.sequence = 0

    def end(self, reason):
        if not self.stop.is_set():
            self.reason = reason
            self.stop.set()

    async def emit(self, value, *, audio=None, response_id=None):
        try:
            await asyncio.wait_for(self.output.put((value, audio, response_id)), 1)
        except TimeoutError:
            raise BridgeStop("backpressure") from None

    async def domain_call(self, name, *args, **kwargs):
        # Audio, SDK events and authorization ticks are independent consumers;
        # serialize this owner's writes (including SQLite's row-lock fallback).
        async with self.domain_lock:
            return await self._db_call(getattr(self.domain, name), *args, **kwargs)

    async def _db_call(self, callback, *args, **kwargs):
        # Never abandon a transaction midway through commit when media stops.
        # Keep the owner's serialization lock until its fenced mutation settled.
        task = asyncio.create_task(callback(*args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.shield(task)
                except Exception:
                    pass
            raise

    def active(self):
        self.last_activity = self.clock()
        self.activity = True

    def _count_frame(self):
        now = self.clock()
        if now - self.window_at >= 1:
            self.window_at, self.window_count = now, 0
        self.window_count += 1
        if self.window_count > 60:
            raise BridgeStop("protocol_error")

    async def receiver(self):
        try:
            while not self.stop.is_set():
                message = await self.socket.receive()
                if message["type"] == "websocket.disconnect":
                    self.end("disconnected")
                    return
                self._count_frame()
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    if not self.ready or not data or len(data) > MAX_FRAME or len(data) % 2:
                        raise BridgeStop("protocol_error")
                    now = self.clock()
                    cap = self.input_rate * 2 * 2
                    self.audio_credit = min(cap, self.audio_credit + (now - self.credit_at) * self.input_rate * 2)
                    self.credit_at = now
                    if len(data) > self.audio_credit:
                        raise BridgeStop("protocol_error")
                    self.audio_credit -= len(data)
                    samples = array("h", data)
                    if sys.byteorder != "little":
                        samples.byteswap()
                    if any(abs(value) > 300 for value in samples):
                        self.active()
                    self._input(("audio", data))
                else:
                    command = _json(message.get("text"))
                    kind = command["type"]
                    if kind == "stop" and set(command) == {"type"}:
                        self.end("stopped")
                        return
                    if kind == "input.commit" and set(command) == {"type"} and self.ready:
                        self.active()
                        self._input(("commit", None))
                    elif kind in {"playback.ack", "interrupt"} and self.ready:
                        if set(command) != {"type", "response_id", "played_bytes"}:
                            raise BridgeStop("protocol_error")
                        self._input((kind, command))
                    else:
                        raise BridgeStop("protocol_error")
        except WebSocketDisconnect:
            self.end("disconnected")

    def _input(self, item):
        try:
            self.input.put_nowait(item)
        except asyncio.QueueFull:
            raise BridgeStop("backpressure") from None

    async def sender(self):
        while not self.stop.is_set():
            value, audio, response = await self.output.get()
            if audio is not None and response in self.suppressed:
                continue
            await asyncio.wait_for(self.socket.send_json(value), 5)
            if audio is not None:
                # Header and PCM remain paired under the single sender.
                # Reserve the upper bound before yielding to the transport: a very
                # fast real playback ACK can race send_bytes returning to this task.
                self.parts[int(response)]["sent"] += len(audio)
                await asyncio.wait_for(self.socket.send_bytes(audio), 5)

    async def _persist(self, index, *, interrupted=False):
        async with self.domain_lock:
            part = self.parts[index]
            interrupted = interrupted or part["interrupted"]
            if not part["text"] or part.get("persisted") == (part["text"], interrupted):
                return
            payload = {"index": index, "speaker": part["speaker"], "text": part["text"],
                       "final": True, "interrupted": interrupted}
            await self._db_call(self.domain.append_transcript, self.session_id, self.owner_id, payload)
            part["persisted"] = (part["text"], interrupted)

    def _part(self, index, speaker):
        if index not in self.parts:
            if len(self.parts) >= 128 or speaker not in {"user", "assistant"}:
                raise BridgeStop("quota_exceeded")
            self.parts[index] = {"speaker": speaker, "text": "", "final": False,
                                 "sent": 0, "acked": 0, "interrupted": False}
        return self.parts[index]

    async def _caption(self, index, text, *, final=False):
        part = self.parts[index]
        if part["interrupted"]:
            return
        if len(text) > MAX_TRANSCRIPT:
            raise BridgeStop("quota_exceeded")
        part["text"], part["final"] = text, final
        await self.emit({"type": "transcript", "index": index, "speaker": part["speaker"], "text": text,
                   "final": final, "interrupted": False})
        if final:
            await self._persist(index)

    async def events(self):
        # This is the sole public session event consumer. No second audio iterator
        # guesses a playback position from SDK consumption or transport progress.
        async for event in self.session:
            if isinstance(event, PartStartEvent) and isinstance(event.part, SpeechPart):
                part = self._part(event.index, event.part.speaker)
                if part["speaker"] == "assistant" and self.pending_interrupt is not None:
                    raise BridgeStop("protocol_error")  # An ACK must never truncate the next turn.
                if event.part.transcript is not None:
                    await self._caption(event.index, event.part.transcript)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, SpeechPartDelta):
                delta = event.delta
                part = self._part(event.index, delta.speaker or self.parts.get(event.index, {}).get("speaker"))
                if delta.transcript is not None or delta.transcript_delta:
                    await self._caption(event.index, delta.transcript if delta.transcript is not None
                                        else part["text"] + delta.transcript_delta)
                if delta.audio_chunk and part["speaker"] == "assistant":
                    response = str(event.index)
                    if response in self.suppressed:
                        continue
                    if self.pending_interrupt is not None:
                        raise BridgeStop("protocol_error")
                    self.current = response
                    self.total_output += len(delta.audio_chunk)
                    if self.total_output > self.output_rate * 2 * 300 or len(delta.audio_chunk) % 2:
                        raise BridgeStop("quota_exceeded")
                    for start in range(0, len(delta.audio_chunk), MAX_FRAME):
                        audio = delta.audio_chunk[start:start + MAX_FRAME]
                        self.sequence += 1
                        await self.emit({"type": "audio.chunk", "sequence": self.sequence, "bytes": len(audio),
                                   "response_id": response}, audio=audio, response_id=response)
            elif isinstance(event, PartEndEvent) and isinstance(event.part, SpeechPart):
                self._part(event.index, event.part.speaker)
                await self._caption(event.index, event.part.transcript or self.parts[event.index]["text"], final=True)
            elif isinstance(event, (RealtimeInputSpeechStartEvent, RealtimeResponseInterruptedEvent)):
                self.active()
                if self.current is not None and self.current not in self.suppressed:
                    self.suppressed.add(self.current)
                    self.pending_interrupt = (self.current, self.clock() + 2)
                    await self.emit({"type": "session.state", "state": "interrupted", "response_id": self.current})
            elif isinstance(event, RealtimeInputSpeechEndEvent):
                await self.emit({"type": "session.state", "state": "thinking"})
            elif isinstance(event, RealtimeTurnCompleteEvent):
                await self.domain_call("record_usage", self.session_id, self.owner_id, _usage(self.usage, False))
                await self.emit({"type": "session.state", "state": "listening"})
            elif isinstance(event, (RealtimeSessionErrorEvent, RealtimeSessionReconnectEvent)):
                raise BridgeStop("provider_error")
        self.end("closed")

    async def _playback(self, command, *, interrupt):
        response, played = command.get("response_id"), command.get("played_bytes")
        if (not isinstance(response, str) or len(response) > 12 or not response.isdecimal()
                or isinstance(played, bool) or not isinstance(played, int) or played < 0 or played % 2):
            raise BridgeStop("protocol_error")
        part = self.parts.get(int(response))
        if part is None or part["speaker"] != "assistant" or not part["acked"] <= played <= part["sent"]:
            raise BridgeStop("protocol_error")
        part["acked"] = played
        if not interrupt:
            return
        if response != self.current:
            raise BridgeStop("protocol_error")
        if part["interrupted"]:
            return
        self.suppressed.add(response)
        # Only browser playback ACKs determine this position. Sent/received bytes
        # are an upper-bound validation, never a claim that audio was heard.
        if self.session.profile.get("supports_output_truncation"):
            await self.session.interrupt(played_ms=played * 1000 // (self.output_rate * 2))
        elif self.session.profile.get("supports_interruption"):
            await self.session.interrupt()
        else:
            raise BridgeStop("protocol_error")
        part["interrupted"] = True
        self.pending_interrupt = None
        await self._persist(int(response), interrupted=True)
        await self.emit({"type": "transcript", "index": int(response), "speaker": "assistant", "text": part["text"],
                   "final": True, "interrupted": True})
        await self.emit({"type": "session.state", "state": "interrupted", "response_id": response})

    async def inputs(self):
        while not self.stop.is_set():
            kind, payload = await self.input.get()
            if kind == "audio":
                await self.session.send_audio(payload)
            elif kind == "commit":
                await self.session.commit_audio()
            else:
                await self._playback(payload, interrupt=kind == "interrupt")

    async def provider(self):
        stack = AsyncExitStack()
        children = []
        try:
            # The SDK owns AnyIO task-group/cancel scopes: enter and exit must
            # happen in this same task. wait_for would create a different task.
            async with asyncio.timeout(12):
                self.session, self.usage = await stack.enter_async_context(self.factory(self.credentials, self.scope))
            self.credentials = None
            scope = await self.domain_call("mark_connected", self.session_id, self.owner_id)
            if not scope.get("allowed", False):
                raise BridgeStop(scope.get("reason") or "permission_revoked")
            self.input_rate = self.session.audio_input_sample_rate
            self.output_rate = self.session.audio_output_sample_rate
            if not 8000 <= self.input_rate <= 96000 or not 8000 <= self.output_rate <= 96000:
                raise BridgeStop("provider_error")
            self.ready = True
            self.audio_credit = self.input_rate * 4
            self.credit_at = self.clock()
            await self.emit({"type": "session.ready", "session_id": self.session_id,
                       "input_sample_rate": self.input_rate, "output_sample_rate": self.output_rate,
                       "channels": 1, "format": "pcm_s16le", "can_interrupt": bool(
                           self.session.profile.get("supports_interruption")
                           and self.session.profile.get("supports_output_truncation")),
                       "max_frame_bytes": MAX_FRAME})
            children = [asyncio.create_task(self.inputs()), asyncio.create_task(self.events()),
                        asyncio.create_task(self.stop.wait())]
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.ready = False
            for task in children:
                task.cancel()
            try:
                await stack.aclose()
                self.cleanup_confirmed = True
            except UsageLimitExceeded:
                # SDK close may report the usage boundary *after* completing its
                # teardown. The enclosing native-model context has also closed.
                self.cleanup_confirmed = True
            except Exception:
                self.cleanup_confirmed = False
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            self.credentials = None

    async def guard(self):
        while not self.stop.is_set():
            await asyncio.sleep(1)
            elapsed = self.clock() - self.started
            if elapsed >= min(300, self.scope.get("remaining_seconds", 300)):
                raise BridgeStop("duration_limit")
            if self.clock() - self.last_activity >= min(60, self.scope.get("idle_seconds", 60)):
                raise BridgeStop("idle_timeout")
            active, self.activity = self.activity, False
            scope = await self.domain_call("heartbeat", self.session_id, self.owner_id, activity=active)
            if not scope.get("allowed", False) or scope.get("stop_requested"):
                raise BridgeStop(scope.get("reason") or "stopped")
            if self.pending_interrupt and self.clock() >= self.pending_interrupt[1] and self.ready:
                response = self.pending_interrupt[0]
                # No fresh ACK: use the last confirmed playhead conservatively.
                self._input(("interrupt", {"response_id": response,
                                          "played_bytes": self.parts[int(response)]["acked"]}))

    async def run(self):
        provider = asyncio.create_task(self.provider())
        self.provider_task = provider
        tasks = [provider, asyncio.create_task(self.receiver()), asyncio.create_task(self.sender()),
                 asyncio.create_task(self.guard()), asyncio.create_task(self.stop.wait())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except BridgeStop as error:
            self.end(error.reason)
        except UsageLimitExceeded:
            self.end("quota_exceeded")
        except DomainError as error:
            self.end("quota_exceeded" if error.status == 429 else "permission_revoked")
        except (WebSocketDisconnect, OSError):
            self.end("disconnected")
        except asyncio.CancelledError:
            self.end("disconnected")
        except Exception:
            self.end("provider_error")
        finally:
            # ASGI servers can cancel the entire request scope on disconnect.
            # Teardown must survive that cancellation, like the SDK's own close.
            with anyio.CancelScope(shield=True):
                await self.finish(tasks)

    async def finish(self, tasks):
        self.stop.set()
        for task in tasks:
            # Once entered, the SDK context is closed normally in its owning
            # task. Cancelling it mid-AnyIO teardown can obscure a completed close.
            # A pending handshake has no yielded session and must be cancelled.
            if task is self.provider_task and self.session is not None:
                continue
            if not task.done():
                task.cancel()
        closed = asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(asyncio.shield(closed), 12)
        except TimeoutError:
            self.cleanup_confirmed = False
            _LATE_CLEANUPS.add(closed)
            closed.add_done_callback(_LATE_CLEANUPS.discard)
        for index, part in self.parts.items():
            if part["text"]:
                try:
                    await self._persist(index, interrupted=part["interrupted"] or not part["final"])
                except Exception:
                    pass
        usage = _usage(self.usage, self.reason in {"closed", "stopped"})
        try:
            await self.domain_call("finish_session", self.session_id, self.owner_id, reason=self.reason,
                                   usage=usage, cleanup_confirmed=self.cleanup_confirmed)
        finally:
            # The sender has stopped; these terminal frames have one writer too.
            try:
                if self.reason not in {"closed", "stopped", "disconnected"}:
                    await self.socket.send_json({"type": "session.error", "code": self.reason,
                        "message": "语音会话已停止，请检查设置或重新开始。"})
                await self.socket.send_json({"type": "session.ended", "reason": self.reason})
            except Exception:
                pass


@router.websocket("/voice-sessions/{session_id}/socket")
async def voice_socket(socket: WebSocket, session_id: str):
    from app.domain import voice as domain

    owner = str(uuid4())
    reserved = False
    try:
        # Authenticate cookie and exact Origin before accepting or consuming tickets.
        if socket.headers.get("origin", "").rstrip("/") != get_settings().public_url.rstrip("/"):
            await socket.close(code=1008)
            return
        login = await domain.authenticate_login(socket.cookies.get(COOKIE, ""))
        await socket.accept()
        frame = await asyncio.wait_for(socket.receive(), 5)
        command = _json(frame.get("text"))
        if (set(command) != {"type", "ticket"} or command["type"] != "authenticate"
                or not isinstance(command["ticket"], str) or not 1 <= len(command["ticket"]) <= 512):
            raise BridgeStop("protocol_error")
        scope = await domain.consume_ticket(login["user_id"], session_id, login["auth_session_id"],
                                            command["ticket"], owner)
        reserved = True
        if not scope.get("allowed", False):
            raise BridgeStop("permission_revoked")
        scope_check = await domain.authorize_session(session_id, owner, login["auth_session_id"])
        if not scope_check.get("allowed", False):
            raise BridgeStop("permission_revoked")
        credentials = await domain.load_credentials(session_id, owner)
        bridge = MediaBridge(socket, domain, session_id, owner, login["auth_session_id"], scope, credentials,
                             factory=voice_session)
        reserved = False  # The bridge now owns fenced finalization and provider cleanup.
        await bridge.run()
        await socket.close(code=1000)
    except (DomainError, BridgeStop, TimeoutError, WebSocketDisconnect):
        if reserved:
            await domain.finish_session(session_id, owner, reason="protocol_error", cleanup_confirmed=True)
        try:
            await socket.close(code=1008)
        except Exception:
            pass
    except Exception:
        if reserved:
            await domain.finish_session(session_id, owner, reason="provider_error", cleanup_confirmed=True)
        try:
            await socket.close(code=1011)
        except Exception:
            pass
