"""Voice control-plane tests. No microphone, SDK provider session, or billable request is used."""

import base64
import asyncio
import json
import os
import secrets
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from app.api import model_configs, routes, voice as api
from app.db.models import (Conversation, DailyBudget, Evaluation, Goal, Message, Session, User, VoiceConfig,
                           VoiceDailyBudget, VoiceSession, VoiceTurn, now)
from app.domain import voice
from app.domain.common import DomainError
from app.integrations import realtime, voice_credentials as vault
from app.integrations.settings import RuntimeSettings
from app.main import app

KEY = "test-only-provider-voice-key-not-real"


@pytest.fixture
def config(tmp_path, monkeypatch):
    master = tmp_path / "voice-master"
    master.write_text(secrets.token_urlsafe(32))
    settings = RuntimeSettings(voice_credential_key_path=master)
    monkeypatch.setattr(voice, "runtime_settings", lambda: settings)
    monkeypatch.setattr(vault, "runtime_settings", lambda: settings)
    admin = SimpleNamespace(model_admin_username="learner")
    monkeypatch.setattr(voice, "get_settings", lambda: admin)
    monkeypatch.setattr(model_configs, "get_settings", lambda: admin)
    return settings


def payload(**changes):
    return api.VoiceConfigInput(expected_version=changes.pop("expected_version", 0), **changes).model_dump()


async def enabled(database):
    return await voice.save_config(database[1].id, payload(provider="openai", model_id="gpt-realtime",
        enabled=True, api_key=SecretStr(KEY)), str(uuid4()))


async def login(database, user=None):
    factory, default, _ = database
    user = user or default
    token = secrets.token_urlsafe(32)
    async with factory() as db, db.begin():
        row = Session(user_id=user.id, token_hash=voice._hash(token), csrf_hash=voice._hash("csrf"),
                      expires_at=now() + timedelta(hours=1))
        db.add(row)
        await db.flush()
        identifier = row.id
    return identifier, token


async def session(database, *, goal=None):
    factory, user, _ = database
    auth_id, token = await login(database)
    if goal:
        async with factory() as db:
            conversation = await db.scalar(select(Conversation).where(Conversation.goal_id == goal["id"]))
        conversation_id = conversation.id
    else:
        conversation_id = (await routes.assistant_conversation(user))["id"]
    item = await voice.create_session(user.id, conversation_id, {"expected_config_version": 1,
        "expected_goal_revision": goal["control_revision"] if goal else None}, str(uuid4()), auth_id)
    return item, auth_id, token


async def connected(database):
    await enabled(database)
    item, auth_id, token = await session(database)
    ticket = await voice.mint_ticket(database[1].id, item["id"], auth_id)
    owner = str(uuid4())
    scope = await voice.consume_ticket(database[1].id, item["id"], auth_id, ticket["ticket"], owner)
    assert scope["allowed"]
    await voice.mark_connected(item["id"], owner)
    return item, auth_id, owner


async def test_draft_save_is_offline_without_master_or_provider_and_cannot_start(database, config, monkeypatch):
    config.voice_credential_key_path.unlink()
    def forbidden(*_, **kwargs):
        raise AssertionError("Saving a draft must never construct a provider")
    monkeypatch.setattr(realtime, "native_model", forbidden)
    factory, user, _ = database
    assert (await voice.capabilities(user.id))["status"] == "unconfigured"
    stored = await voice.save_config(user.id, payload(), "save-draft-once")
    assert stored["provider"] is None and stored["model_id"] is None and not stored["key_configured"]
    assert not stored["enabled"]
    assert await voice.save_config(user.id, payload(), "save-draft-once") == stored
    with pytest.raises(DomainError) as error:
        await session(database)
    assert error.value.code == "voice_not_ready"
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(VoiceSession)) == 0


async def test_aesgcm_ciphertext_context_binding_rotation_and_no_secret_in_public_dto(database, config):
    stored = await enabled(database)
    assert KEY not in json.dumps(stored) and stored["key_configured"]
    factory, user, _ = database
    async with factory() as db:
        row = await db.get(VoiceConfig, voice.CONFIG_ID)
        ciphertext, nonce, key_id = row.credential_ciphertext, row.credential_nonce, row.credential_key_id
        assert KEY.encode() not in ciphertext and len(nonce) == 12
        assert vault.open_secret(ciphertext, nonce, key_id, row.id, row.provider, row.version).get_secret_value() == KEY
        for changed in [(row.id, "google", row.version), ("other-config", row.provider, row.version),
                        (row.id, row.provider, row.version + 1)]:
            with pytest.raises(DomainError):
                vault.open_secret(ciphertext, nonce, key_id, *changed)
    updated = await voice.save_config(user.id, payload(expected_version=1, provider="openai", model_id="gpt-realtime",
        enabled=False), str(uuid4()))
    assert updated["version"] == 2 and updated["key_configured"]
    async with factory() as db:
        row = await db.get(VoiceConfig, voice.CONFIG_ID)
        assert row.credential_ciphertext != ciphertext and row.credential_nonce != nonce
        assert vault.open_secret(row.credential_ciphertext, row.credential_nonce, row.credential_key_id,
                                 row.id, row.provider, row.version).get_secret_value() == KEY
    switched = await voice.save_config(user.id, payload(expected_version=2, provider="google", model_id="gemini-live"), str(uuid4()))
    assert not switched["key_configured"] and not switched["enabled"]
    assert (await voice.capabilities(user.id))["status"] == "unconfigured"


@pytest.mark.parametrize("raw", ["short", "x" * 129, "x" * 43 + "$", base64.urlsafe_b64encode(b"x" * 31).decode()])
def test_master_key_encoding_is_strict_and_redacted(config, raw):
    config.voice_credential_key_path.write_text(raw)
    with pytest.raises(DomainError) as error:
        vault.seal(SecretStr(KEY), "id", "openai", 1)
    assert raw not in error.value.message and KEY not in error.value.message


async def test_provider_dependencies_and_disabled_state_are_local(database, config, monkeypatch):
    await enabled(database)
    monkeypatch.setattr(voice, "_provider", lambda _: {"supported": True, "dependency_ready": False,
                                                       "reason": "dependency_unavailable"})
    assert (await voice.capabilities(database[1].id))["status"] == "dependency_unavailable"
    config.voice_enabled = False
    assert (await voice.capabilities(database[1].id))["status"] == "disabled"


@pytest.mark.parametrize("token", [None, "", "x" * 513, "\ud800"])
async def test_invalid_login_cookie_is_rejected_before_database_access(token):
    with pytest.raises(DomainError) as error:
        await voice.authenticate_login(token)
    assert error.value.status == 401


async def test_ticket_is_single_use_bound_to_login_user_config_and_expiry(database, config):
    await enabled(database)
    item, auth_id, _ = await session(database)
    _, user, other = database
    ticket = await voice.mint_ticket(user.id, item["id"], auth_id)
    assert "?" not in ticket["websocket_path"] and ticket["ticket"] not in ticket["websocket_path"]
    async with database[0]() as db:
        row = await db.get(VoiceSession, item["id"])
        assert row.ticket_hash == voice._hash(ticket["ticket"]) and row.ticket_hash != ticket["ticket"]
    other_auth, _ = await login(database)
    with pytest.raises(DomainError):
        await voice.consume_ticket(user.id, item["id"], other_auth, ticket["ticket"], str(uuid4()))
    with pytest.raises(DomainError):
        await voice.consume_ticket(other.id, item["id"], auth_id, ticket["ticket"], str(uuid4()))
    owner = str(uuid4())
    scope = await voice.consume_ticket(user.id, item["id"], auth_id, ticket["ticket"], owner)
    assert scope["allowed"] and scope["status"] == "connecting"
    with pytest.raises(DomainError) as error:
        await voice.consume_ticket(user.id, item["id"], auth_id, ticket["ticket"], str(uuid4()))
    assert error.value.code == "voice_ticket_invalid"
    credentials = await voice.load_credentials(item["id"], owner)
    assert isinstance(credentials["api_key"], SecretStr) and credentials["api_key"].get_secret_value() == KEY
    assert credentials["base_url"] == "https://api.openai.com/v1"
    with pytest.raises(DomainError):
        await voice.load_credentials(item["id"], str(uuid4()))


async def test_expired_ticket_cannot_connect(database, config):
    await enabled(database)
    item, auth_id, _ = await session(database)
    ticket = await voice.mint_ticket(database[1].id, item["id"], auth_id)
    async with database[0]() as db, db.begin():
        (await db.get(VoiceSession, item["id"])).ticket_expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError):
        await voice.consume_ticket(database[1].id, item["id"], auth_id, ticket["ticket"], str(uuid4()))


@pytest.mark.skipif(not os.getenv("APP_TEST_DATABASE_URL"), reason="PostgreSQL row-lock race requires APP_TEST_DATABASE_URL")
async def test_real_postgres_race_consumes_ticket_exactly_once(database, config):
    await enabled(database)
    item, auth_id, _ = await session(database)
    ticket = await voice.mint_ticket(database[1].id, item["id"], auth_id)
    results = await asyncio.gather(*(voice.consume_ticket(database[1].id, item["id"], auth_id,
        ticket["ticket"], str(uuid4())) for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(result, dict) and result["allowed"] for result in results) == 1
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 1 and isinstance(failures[0], DomainError) and failures[0].code == "voice_ticket_invalid"


@pytest.mark.parametrize("change", ["logout", "account", "memory", "config", "deadline", "idle", "lease"])
async def test_session_scope_revocation_and_owner_fence(database, config, change):
    item, auth_id, owner = await connected(database)
    factory, user, _ = database
    async with factory() as db, db.begin():
        row = await db.get(VoiceSession, item["id"])
        if change == "logout":
            (await db.get(Session, auth_id)).revoked = True
        elif change == "account":
            (await db.get(User, user.id)).active = False
        elif change == "memory":
            (await db.get(User, user.id)).memory_control_revision += 1
        elif change == "config":
            (await db.get(VoiceConfig, voice.CONFIG_ID)).version += 1
        elif change == "deadline":
            row.deadline_at = now() - timedelta(seconds=1)
        elif change == "idle":
            row.last_activity_at = now() - timedelta(seconds=61)
        else:
            row.lease_until = now() - timedelta(seconds=1)
    scope = await voice.heartbeat(item["id"], owner, activity=True)
    assert not scope["allowed"] and scope["stop_requested"]
    with pytest.raises(DomainError):
        await voice.authorize_session(item["id"], str(uuid4()), auth_id)


async def test_goal_revision_and_pause_revoke_voice_scope(database, config):
    from test_domain import active_goal
    await enabled(database)
    goal, _, _ = await active_goal(database[1])
    item, auth_id, _ = await session(database, goal=goal)
    ticket = await voice.mint_ticket(database[1].id, item["id"], auth_id)
    owner = str(uuid4())
    await voice.consume_ticket(database[1].id, item["id"], auth_id, ticket["ticket"], owner)
    async with database[0]() as db, db.begin():
        (await db.get(Goal, goal["id"])).status = "paused"
    assert not (await voice.authorize_session(item["id"], owner, auth_id))["allowed"]


async def test_reservation_concurrency_stop_and_quota_are_separate_from_learning(database, config):
    await enabled(database)
    item, auth_id, _ = await session(database)
    capabilities = await voice.capabilities(database[1].id, item["conversation_id"])
    assert capabilities["budget"] == {"available_seconds": 600, "reserved_seconds": 300, "consumed_seconds": 0}
    assert not capabilities["can_start"] and capabilities["start_reason"] == "voice_busy"
    with pytest.raises(DomainError) as error:
        await session(database)
    assert error.value.code == "voice_busy"
    closed = await voice.request_stop(database[1].id, item["id"], "stop-pending")
    assert closed["cleanup_done"] and closed["status"] == "closed"
    assert (await voice.capabilities(database[1].id))["budget"]["available_seconds"] == 900
    async with database[0]() as db:
        assert await db.scalar(select(func.count()).select_from(DailyBudget)) == 0


async def test_daily_quota_has_a_hard_limit_and_pending_expiry_refunds(database, config):
    await enabled(database)
    item, auth_id, _ = await session(database)
    async with database[0]() as db, db.begin():
        (await db.get(VoiceSession, item["id"])).pending_expires_at = now() - timedelta(seconds=1)
    assert (await voice.capabilities(database[1].id))["budget"]["available_seconds"] == 900
    for _ in range(3):
        current, auth_id, _ = await session(database)
        ticket = await voice.mint_ticket(database[1].id, current["id"], auth_id)
        owner = str(uuid4())
        await voice.consume_ticket(database[1].id, current["id"], auth_id, ticket["ticket"], owner)
        async with database[0]() as db, db.begin():
            row = await db.get(VoiceSession, current["id"])
            row.started_at = now() - timedelta(seconds=301)
        await voice.finish_session(current["id"], owner, reason="duration_limit")
    caps = await voice.capabilities(database[1].id, current["conversation_id"])
    assert caps["budget"]["consumed_seconds"] == 900 and not caps["can_start"]
    assert caps["start_reason"] == "voice_budget_exceeded"
    with pytest.raises(DomainError) as error:
        await session(database)
    assert error.value.code == "voice_budget_exceeded"


async def test_ciphertext_tampering_and_master_replacement_are_detected_locally(database, config):
    item, auth_id, owner = await connected(database)
    async with database[0]() as db, db.begin():
        row = await db.get(VoiceConfig, voice.CONFIG_ID)
        row.credential_ciphertext = row.credential_ciphertext[:-1] + bytes([row.credential_ciphertext[-1] ^ 1])
    with pytest.raises(DomainError) as error:
        await voice.load_credentials(item["id"], owner)
    assert error.value.code == "voice_credentials_unavailable" and KEY not in error.value.message
    config.voice_credential_key_path.write_text(secrets.token_urlsafe(32))
    assert (await voice.capabilities(database[1].id))["status"] == "dependency_unavailable"


async def test_admin_versions_idempotency_and_dependency_unavailable_do_not_enable(database, config, monkeypatch):
    configured = await enabled(database)
    with pytest.raises(DomainError) as error:
        await voice.save_config(database[1].id, payload(expected_version=0), str(uuid4()))
    assert error.value.code == "version_conflict"
    with pytest.raises(DomainError) as error:
        await voice.save_config(database[2].id, payload(expected_version=1), str(uuid4()))
    assert error.value.code == "forbidden"
    monkeypatch.setattr(voice, "_provider", lambda _: {"supported": True, "dependency_ready": False,
                                                       "reason": "dependency_unavailable"})
    with pytest.raises(DomainError):
        await voice.save_config(database[1].id, payload(expected_version=1, provider="google", model_id="gemini-live",
            api_key=SecretStr("different-non-real-key"), enabled=True), str(uuid4()))
    assert await voice.get_config() == configured


async def test_transcripts_upsert_recover_and_usage_settle_only_once_without_business_writes(database, config):
    item, auth_id, owner = await connected(database)
    for content, final in [("你好", False), ("你好世界", True), ("旧片段", False), ("你好世界。", True)]:
        await voice.append_transcript(item["id"], owner, {"index": 3, "speaker": "user", "text": content, "final": final})
    await voice.append_transcript(item["id"], owner, {"index": 4, "speaker": "assistant", "text": "你好", "final": True})
    await voice.record_usage(item["id"], owner, {"requests": 2, "input_tokens": 10, "output_tokens": 15, "secret": KEY})
    first = await voice.finish_session(item["id"], owner, usage={"requests": 2, "input_tokens": 10,
                                                               "output_tokens": 15, "usage_complete": True})
    assert first["usage"]["total_tokens"] == 25 and first["usage"]["usage_complete"]
    assert first["transcripts"][0]["text"] == "你好世界。" and first["transcripts"][0]["final"]
    again = await voice.finish_session(item["id"], owner)
    assert again["duration_seconds"] == first["duration_seconds"]
    recovered = await voice.capabilities(database[1].id, item["conversation_id"])
    assert recovered["latest_session"]["transcripts"] == first["transcripts"]
    assert KEY not in json.dumps(first)
    async with database[0]() as db:
        assert await db.scalar(select(func.count()).select_from(VoiceTurn)) == 2
        budget = await db.scalar(select(VoiceDailyBudget))
        assert budget.reserved_seconds == 0 and budget.consumed_seconds == first["duration_seconds"]
        assert await db.scalar(select(func.count()).select_from(Message)) == 0
        assert await db.scalar(select(func.count()).select_from(Evaluation)) == 0


async def test_stale_connection_charges_conservatively_then_accepts_late_cleanup(database, config):
    item, auth_id, owner = await connected(database)
    async with database[0]() as db, db.begin():
        (await db.get(VoiceSession, item["id"])).lease_until = now() - timedelta(seconds=1)
    caps = await voice.capabilities(database[1].id)
    assert caps["budget"] == {"reserved_seconds": 0, "consumed_seconds": 300, "available_seconds": 600}
    stale = await voice.get_session(database[1].id, item["id"])
    assert stale["status"] == "interrupted" and not stale["cleanup_done"]
    result = await voice.finish_session(item["id"], owner)
    assert result["cleanup_done"] and result["reason"] == "lease_expired"
    assert (await voice.capabilities(database[1].id))["budget"]["consumed_seconds"] == 300


async def test_completed_session_scope_keeps_original_reason_after_configuration_changes(database, config):
    item, auth_id, owner = await connected(database)
    await voice.finish_session(item["id"], owner, reason="stopped")
    await voice.save_config(database[1].id, payload(expected_version=1, enabled=False), str(uuid4()))
    scope = await voice.authorize_session(item["id"], owner, auth_id)
    assert not scope["allowed"] and scope["reason"] == "stopped" and scope["remaining_seconds"] == 0
    assert (await voice.get_session(database[1].id, item["id"]))["reason"] == "stopped"


@pytest.mark.parametrize("field,value", [("voice_max_session_seconds", 301), ("voice_idle_seconds", 61)])
def test_voice_settings_match_bridge_hard_limits(field, value):
    with pytest.raises(ValidationError):
        RuntimeSettings(**{field: value})


async def test_http_auth_csrf_admin_strict_input_and_secret_write_only(database, config):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        assert (await client.get("/api/v1/voice/capabilities")).status_code == 401
        response = await client.post("/api/v1/auth/login", json={"username": "learner", "password": "test-only-long-password"},
                                     headers={"Origin": "http://localhost:8080"})
        headers = {"Origin": "http://localhost:8080", "X-CSRF-Token": response.json()["csrf_token"],
                   "Idempotency-Key": "voice-http-config"}
        body = {"expected_version": 0, "provider": "openai", "model_id": "gpt-realtime", "api_key": KEY, "enabled": True}
        assert (await client.put("/api/v1/voice/config", json=body)).status_code == 403
        configured = await client.put("/api/v1/voice/config", json=body, headers=headers)
        assert configured.status_code == 200, configured.text
        assert KEY not in configured.text and "ciphertext" not in configured.text and "credential" not in configured.text
        assert (await client.get("/api/v1/voice/capabilities")).json()["status"] == "ready"
        invalid = await client.put("/api/v1/voice/config", json={**body, "api_base": "https://evil.example"}, headers=headers)
        assert invalid.status_code == 422 and KEY not in invalid.text
        conversation = (await routes.assistant_conversation(database[1]))["id"]
        headers["Idempotency-Key"] = "voice-http-session"
        created = await client.post(f"/api/v1/conversations/{conversation}/voice-sessions",
            json={"expected_config_version": 1}, headers=headers)
        assert created.status_code == 201, created.text
        item = created.json()
        ticket = await client.post(f"/api/v1/voice-sessions/{item['id']}/connect-ticket", json={}, headers=headers)
        assert ticket.status_code == 200 and len(ticket.json()["ticket"]) >= 32
        assert "owner_id" not in item and "ticket_hash" not in item and "auth_session_id" not in item
        stopped = await client.post(f"/api/v1/voice-sessions/{item['id']}/stop", json={}, headers=headers)
        assert stopped.status_code == 200 and stopped.json()["cleanup_done"]
        assert (await client.get(f"/api/v1/voice-sessions/{item['id']}")).json()["status"] == "closed"


@pytest.mark.parametrize("endpoint", ["http://a.openai.azure.com", "https://127.0.0.1", "https://a.openai.azure.com.evil.test",
    "https://user:pass@a.openai.azure.com", "https://a.openai.azure.com/path", "https://a.openai.azure.com?key=x"])
def test_only_explicit_official_azure_endpoint_is_accepted(endpoint):
    with pytest.raises(ValidationError):
        api.VoiceConfigInput(expected_version=0, provider="azure", endpoint=endpoint)
