"""Local voice control plane. Audio and provider sockets never enter this module or Temporal."""

import hashlib
import math
import secrets
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from sqlalchemy import and_, func, or_, select, update

from app.config import get_settings
from app.db.models import (Conversation, Goal, Message, Session, VoiceConfig, VoiceDailyBudget,
                           VoiceSession, VoiceTurn, now)
from app.db.session import SessionFactory
from app.domain.common import DomainError, digest, emit, lock_user, owned, serialize, version
from app.integrations import voice_credentials as vault
from app.integrations.settings import runtime_settings

CONFIG_ID = "00000000-0000-4000-8000-000000000008"
ACTIVE = {"pending", "connecting", "active", "stopping"}
FINAL = {"closed", "failed", "interrupted", "expired"}
REASONS = {"closed", "stopped", "disconnected", "idle_timeout", "duration_limit", "quota_exceeded",
           "permission_revoked", "configuration_changed", "provider_error", "protocol_error", "backpressure",
           "interrupted", "lease_expired", "ticket_expired"}
MESSAGES = {"provider_error": "语音供应商连接未完成，请检查配置后重试", "protocol_error": "语音连接数据无效，已结束本次会话",
    "backpressure": "语音传输未能及时完成，已停止会话", "permission_revoked": "当前语音会话授权已变化，已停止连接",
    "configuration_changed": "语音配置已变化，请重新开始会话", "lease_expired": "语音连接已中断，额度按保守上限结算",
    "duration_limit": "本次语音会话已达到时长上限", "idle_timeout": "语音会话长时间没有活动，已停止连接"}


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _providers():
    from app.integrations.realtime import public_provider_options
    return public_provider_options()


def _provider(provider):
    from app.integrations.realtime import provider_readiness
    return provider_readiness(provider)


def public_config(row):
    if row is None:
        return {"version": 0, "provider": None, "model_id": None, "voice": None, "endpoint": None,
                "api_version": None, "enabled": False, "key_configured": False, "updated_at": None}
    return {"version": row.version, "provider": row.provider, "model_id": row.model_id, "voice": row.voice,
            "endpoint": row.endpoint, "api_version": row.api_version, "enabled": row.enabled,
            "key_configured": row.credential_ciphertext is not None, "updated_at": _aware(row.updated_at).isoformat()}


def _config_status(config):
    if not runtime_settings().voice_enabled:
        return "disabled", "feature_disabled"
    if (config is None or not config.provider or not config.model_id or not config.credential_ciphertext
            or config.provider == "azure" and (not config.endpoint or not config.api_version)):
        return "unconfigured", "provider_not_configured"
    info = _provider(config.provider)
    if not info["supported"] or not info["dependency_ready"]:
        return "dependency_unavailable", info["reason"]
    if not vault.ready(config.credential_key_id):
        return "dependency_unavailable", "credentials_unavailable"
    return ("ready", "ready") if config.enabled else ("disabled", "configuration_disabled")


async def get_config():
    async with SessionFactory() as db:
        return public_config(await db.get(VoiceConfig, CONFIG_ID))


async def save_config(user_id, payload, key):
    payload = dict(payload)
    secret = payload.pop("api_key", None)
    if secret is not None and not isinstance(secret, SecretStr):
        secret = SecretStr(secret)
    fingerprint = digest({**payload, "api_key": secret.get_secret_value() if secret else None})
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        if not user.active or not get_settings().model_admin_username or user.username != get_settings().model_admin_username:
            raise DomainError("forbidden", "仅部署管理员可以管理语音配置", 403)
        row = await db.scalar(select(VoiceConfig).where(VoiceConfig.id == CONFIG_ID).with_for_update())
        if row and row.last_request_key == key:
            if row.last_request_hash != fingerprint:
                raise DomainError("idempotency_conflict", "该请求标识已用于不同语音配置")
            return public_config(row)
        version(row.version if row else 0, payload["expected_version"])
        provider = payload["provider"]
        if provider is not None and not _provider(provider)["supported"]:
            raise DomainError("voice_provider_unsupported", "当前版本尚不支持此语音供应商", 422)
        if provider is None and (secret or payload.get("model_id") or payload.get("voice") or payload.get("endpoint")
                                 or payload.get("api_version") or payload["enabled"]):
            raise DomainError("voice_draft_invalid", "未选择语音供应商时，请保留空配置并关闭启用", 422)
        if provider != "azure" and (payload.get("endpoint") or payload.get("api_version")):
            raise DomainError("voice_endpoint_invalid", "当前供应商使用固定官方地址，无需自定义地址", 422)
        next_version = row.version + 1 if row else 1
        preserved = None
        same_scope = row and row.provider == provider and row.endpoint == payload.get("endpoint")
        if secret is None and same_scope and row.credential_ciphertext:
            preserved = vault.open_secret(row.credential_ciphertext, row.credential_nonce, row.credential_key_id,
                                         row.id, row.provider, row.version)
        selected_secret = secret or preserved
        if row is None:
            row = VoiceConfig(id=CONFIG_ID, last_request_key=key, last_request_hash=fingerprint)
            db.add(row)
        for name in ("provider", "model_id", "voice", "endpoint", "api_version", "enabled"):
            setattr(row, name, payload.get(name))
        row.version, row.updated_at = next_version, now()
        row.credential_ciphertext = row.credential_nonce = row.credential_key_id = None
        if selected_secret is not None:
            row.credential_ciphertext, row.credential_nonce, row.credential_key_id = vault.seal(
                selected_secret, row.id, provider, row.version)
        if row.enabled and _config_status(row)[0] != "ready":
            raise DomainError("voice_not_ready", "请先补全供应商、模型、密钥及所需本机依赖，再启用语音", 422)
        row.last_request_key, row.last_request_hash = key, fingerprint
        await db.flush()
        await emit(db, stream, "voice.configured", row.id, "语音配置已在本机保存，未连接供应商", entity_version=row.version)
        return public_config(row)


async def authenticate_login(cookie_token):
    if not isinstance(cookie_token, str) or not cookie_token or not cookie_token.isascii() or len(cookie_token) > 512:
        raise DomainError("unauthenticated", "请先登录", 401)
    async with SessionFactory() as db:
        auth = await db.scalar(select(Session).where(Session.token_hash == _hash(cookie_token),
            Session.revoked.is_(False), Session.expires_at > now()))
        if not auth:
            raise DomainError("unauthenticated", "登录已过期", 401)
        from app.db.models import User
        user = await db.get(User, auth.user_id)
        if not user or not user.active:
            raise DomainError("unauthenticated", "账号已停用", 401)
        return {"user_id": user.id, "auth_session_id": auth.id}


async def _login(db, user, auth_id):
    auth = await db.get(Session, auth_id)
    return bool(user.active and auth and auth.user_id == user.id and not auth.revoked and _aware(auth.expires_at) > now())


async def _budget(db, user_id, day):
    row = await db.get(VoiceDailyBudget, (user_id, day))
    if row is None:
        row = VoiceDailyBudget(user_id=user_id, local_date=day, reserved_seconds=0, consumed_seconds=0)
        db.add(row)
        await db.flush()
    return row


async def _inflight(db, user_id):
    return await db.scalar(select(func.count()).select_from(VoiceSession).where(VoiceSession.user_id == user_id,
        or_(VoiceSession.status.in_(ACTIVE), and_(VoiceSession.started_at.is_not(None),
            VoiceSession.cleanup_done.is_(False), VoiceSession.deadline_at > now()))))


def _duration(item):
    if item.started_at is None:
        return 0
    end = _aware(item.closed_at) if item.closed_at else now()
    return min(item.max_seconds, max(0, math.ceil((end - _aware(item.started_at)).total_seconds())))


async def _settle(db, item, *, conservative=False):
    if item.quota_settled:
        return
    row = await _budget(db, item.user_id, item.local_date)
    charged = item.max_seconds if conservative and item.started_at else _duration(item)
    row.reserved_seconds = max(0, row.reserved_seconds - item.reserved_seconds)
    row.consumed_seconds += charged
    item.consumed_seconds, item.quota_settled = charged, True


async def _changed(db, item, stream, summary):
    item.updated_at = now()
    await emit(db, stream, "voice.session_updated", item.id, summary, item.goal_id)


async def _recover(db, user, stream):
    rows = await db.scalars(select(VoiceSession).where(VoiceSession.user_id == user.id, VoiceSession.status.in_(ACTIVE))
                            .execution_options(populate_existing=True))
    for item in rows:
        if item.status == "pending" and _aware(item.pending_expires_at) <= now():
            item.status, item.reason, item.cleanup_done, item.closed_at = "expired", "ticket_expired", True, now()
            item.stop_requested = True
            await _settle(db, item)
            await _changed(db, item, stream, "未连接的语音会话已过期，预留额度已释放")
        elif item.started_at and item.lease_until and _aware(item.lease_until) <= now():
            item.status, item.reason, item.closed_at, item.stop_requested = "interrupted", "lease_expired", now(), True
            item.error_message = MESSAGES["lease_expired"]
            await _settle(db, item, conservative=True)
            await _changed(db, item, stream, "语音连接已中断，等待连接清理确认")


async def public_session(db, item):
    rows = await db.scalars(select(VoiceTurn).where(VoiceTurn.session_id == item.id, VoiceTurn.user_id == item.user_id)
                            .order_by(VoiceTurn.index, VoiceTurn.speaker).limit(100))
    duration = _duration(item)
    return {"id": item.id, "conversation_id": item.conversation_id, "goal_id": item.goal_id,
        "status": item.status, "config": {key: item.config_snapshot.get(key) for key in ("version", "provider", "model_id", "voice")},
        "max_seconds": item.max_seconds, "duration_seconds": duration,
        "remaining_seconds": max(0, item.max_seconds - duration) if item.status in ACTIVE else 0,
        "created_at": _aware(item.created_at).isoformat(), "updated_at": _aware(item.updated_at).isoformat(),
        "started_at": _aware(item.started_at).isoformat() if item.started_at else None,
        "closed_at": _aware(item.closed_at).isoformat() if item.closed_at else None,
        "stop_requested": item.stop_requested, "cleanup_done": item.cleanup_done,
        "reason": item.reason, "error_message": item.error_message,
        "usage": {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                  "usage_complete": False, **item.usage},
        "transcripts": [serialize(row, {"id", "session_id", "user_id", "created_at"}) for row in rows]}


async def capabilities(user_id, conversation_id=None):
    settings = runtime_settings()
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        await _recover(db, user, stream)
        config = await db.get(VoiceConfig, CONFIG_ID)
        status, reason = _config_status(config)
        scope_ready = False
        if conversation_id:
            conversation = await owned(db, Conversation, conversation_id, user_id)
            goal = await db.get(Goal, conversation.goal_id) if conversation.goal_id else None
            scope_ready = goal is None or goal.user_id == user_id and goal.status in {"draft", "active"}
        day = now().astimezone(ZoneInfo(user.timezone)).date()
        budget = await _budget(db, user_id, day)
        available = max(0, settings.voice_daily_seconds - budget.reserved_seconds - budget.consumed_seconds)
        busy = await _inflight(db, user_id) >= settings.voice_max_concurrent
        start_reason = (reason if status != "ready" else "conversation_unavailable" if not scope_ready or not user.active
                        else "voice_busy" if busy else "voice_budget_exceeded" if available < settings.voice_max_session_seconds else "ready")
        latest = await db.scalar(select(VoiceSession).where(VoiceSession.user_id == user_id,
            VoiceSession.conversation_id == conversation_id).order_by(VoiceSession.created_at.desc()).limit(1)) if conversation_id else None
        return {"status": status, "reason": reason, "can_manage": user.username == get_settings().model_admin_username,
            "can_start": start_reason == "ready", "start_reason": start_reason, "config": public_config(config),
            "providers": _providers(), "latest_session": await public_session(db, latest) if latest else None,
            "limits": {"max_session_seconds": settings.voice_max_session_seconds,
                "daily_seconds": settings.voice_daily_seconds, "idle_seconds": settings.voice_idle_seconds,
                "max_concurrent": settings.voice_max_concurrent},
            "budget": {"reserved_seconds": budget.reserved_seconds, "consumed_seconds": budget.consumed_seconds,
                "available_seconds": available}}


async def create_session(user_id, conversation_id, payload, key, auth_session_id):
    settings = runtime_settings()
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        if not await _login(db, user, auth_session_id):
            raise DomainError("unauthenticated", "登录已过期", 401)
        conversation = await owned(db, Conversation, conversation_id, user_id)
        fingerprint = digest({**payload, "conversation_id": conversation_id, "auth_session_id": auth_session_id})
        existing = await db.scalar(select(VoiceSession).where(VoiceSession.user_id == user_id, VoiceSession.idempotency_key == key))
        if existing:
            if existing.request_hash != fingerprint:
                raise DomainError("idempotency_conflict", "该请求标识已用于不同语音会话")
            return await public_session(db, existing)
        await _recover(db, user, stream)
        config = await db.get(VoiceConfig, CONFIG_ID)
        if _config_status(config)[0] != "ready":
            raise DomainError("voice_not_ready", "请先在语音设置中选择供应商并完成配置", 409)
        version(config.version, payload["expected_config_version"])
        goal = await db.get(Goal, conversation.goal_id) if conversation.goal_id else None
        if goal and (goal.user_id != user_id or goal.status not in {"draft", "active"}):
            raise DomainError("voice_scope_closed", "当前目标不允许启动语音会话", 409)
        if goal:
            version(goal.control_revision, payload.get("expected_goal_revision"))
        count = await _inflight(db, user_id)
        if count >= settings.voice_max_concurrent:
            raise DomainError("voice_busy", "已有语音会话正在连接或清理，请结束后重试", 429)
        day = now().astimezone(ZoneInfo(user.timezone)).date()
        budget = await _budget(db, user_id, day)
        if budget.reserved_seconds + budget.consumed_seconds + settings.voice_max_session_seconds > settings.voice_daily_seconds:
            raise DomainError("voice_budget_exceeded", "今日语音额度不足以预留本次会话", 429)
        budget.reserved_seconds += settings.voice_max_session_seconds
        item = VoiceSession(user_id=user_id, auth_session_id=auth_session_id, conversation_id=conversation_id,
            goal_id=goal.id if goal else None, goal_revision=goal.control_revision if goal else None,
            memory_revision=user.memory_control_revision, config_id=config.id, config_version=config.version,
            config_snapshot=public_config(config), local_date=day, idempotency_key=key, request_hash=fingerprint,
            max_seconds=settings.voice_max_session_seconds, reserved_seconds=settings.voice_max_session_seconds,
            pending_expires_at=now() + timedelta(seconds=60), usage={})
        db.add(item)
        await db.flush()
        await _changed(db, item, stream, "语音会话已准备，等待本次连接认证")
        return await public_session(db, item)


async def _locked(db, session_id):
    initial = await db.get(VoiceSession, session_id)
    if not initial:
        raise DomainError("not_found", "语音会话不存在", 404)
    user, stream = await lock_user(db, initial.user_id)
    item = await db.scalar(select(VoiceSession).where(VoiceSession.id == session_id).with_for_update()
                           .execution_options(populate_existing=True))
    return item, user, stream


async def get_session(user_id, session_id):
    async with SessionFactory() as db, db.begin():
        item = await owned(db, VoiceSession, session_id, user_id)
        user, stream = await lock_user(db, user_id)
        await _recover(db, user, stream)
        return await public_session(db, item)


async def mint_ticket(user_id, session_id, auth_session_id):
    async with SessionFactory() as db, db.begin():
        await owned(db, VoiceSession, session_id, user_id)
        item, user, stream = await _locked(db, session_id)
        if not await _login(db, user, auth_session_id) or item.auth_session_id != auth_session_id:
            raise DomainError("unauthenticated", "此语音会话不属于当前登录", 401)
        if item.status != "pending" or _aware(item.pending_expires_at) <= now():
            raise DomainError("voice_session_closed", "本次连接已开始或过期，请重新创建语音会话", 409)
        token = secrets.token_urlsafe(32)
        item.ticket_hash, item.ticket_consumed_at = _hash(token), None
        item.ticket_expires_at = min(now() + timedelta(seconds=30), _aware(item.pending_expires_at))
        return {"ticket": token, "expires_at": item.ticket_expires_at.isoformat(),
                "websocket_path": f"/api/v1/voice-sessions/{item.id}/socket"}


async def _scope(db, item, user):
    config = await db.get(VoiceConfig, item.config_id)
    conversation = await db.get(Conversation, item.conversation_id)
    goal = await db.get(Goal, item.goal_id) if item.goal_id else None
    reason = None
    if item.status in FINAL:
        reason = item.reason or "closed"
    elif (not await _login(db, user, item.auth_session_id) or not conversation or conversation.user_id != user.id
            or conversation.goal_id != item.goal_id):
        reason = "permission_revoked"
    elif user.memory_control_revision != item.memory_revision:
        reason = "permission_revoked"
    elif item.goal_id and (not goal or goal.user_id != user.id or goal.status not in {"draft", "active"}
                          or goal.control_revision != item.goal_revision):
        reason = "permission_revoked"
    elif not config or config.version != item.config_version or _config_status(config)[0] != "ready":
        reason = "configuration_changed"
    elif item.deadline_at and _aware(item.deadline_at) <= now():
        reason = "duration_limit"
    elif item.last_activity_at and (now() - _aware(item.last_activity_at)).total_seconds() >= runtime_settings().voice_idle_seconds:
        reason = "idle_timeout"
    elif item.stop_requested:
        reason = item.reason or "stopped"
    elif item.owner_id and item.lease_until and _aware(item.lease_until) <= now():
        reason = "lease_expired"
    if reason and item.status not in FINAL:
        item.stop_requested = True
        item.status = "stopping"
        item.reason, item.error_message = reason, MESSAGES.get(reason)
    remaining = 0 if item.status in FINAL else (
        max(0, (_aware(item.deadline_at) - now()).total_seconds()) if item.deadline_at else item.max_seconds)
    return {"session_id": item.id, "user_id": user.id, "auth_session_id": item.auth_session_id,
        "conversation_id": item.conversation_id, "goal_id": item.goal_id, "config_version": item.config_version,
        "provider": item.config_snapshot["provider"], "model_id": item.config_snapshot["model_id"],
        "voice": item.config_snapshot.get("voice"), "allowed": reason is None,
        "reason": reason, "status": item.status, "stop_requested": item.stop_requested,
        "max_seconds": item.max_seconds, "remaining_seconds": remaining,
        "idle_seconds": runtime_settings().voice_idle_seconds, "cleanup_done": item.cleanup_done}


async def consume_ticket(user_id, session_id, auth_session_id, ticket, owner_id):
    async with SessionFactory() as db, db.begin():
        await owned(db, VoiceSession, session_id, user_id)
        item, user, stream = await _locked(db, session_id)
        if (item.auth_session_id != auth_session_id or not isinstance(owner_id, str) or not owner_id.isascii()
                or not 8 <= len(owner_id) <= 100
                or item.status != "pending" or item.ticket_consumed_at or not item.ticket_hash
                or not isinstance(ticket, str) or not ticket.isascii() or len(ticket) > 256
                or not secrets.compare_digest(item.ticket_hash, _hash(ticket))
                or not item.ticket_expires_at or _aware(item.ticket_expires_at) <= now()
                or _aware(item.pending_expires_at) <= now()):
            raise DomainError("voice_ticket_invalid", "语音连接凭据已失效或已被使用", 403)
        scope = await _scope(db, item, user)
        if not scope["allowed"]:
            raise DomainError("voice_scope_closed", "语音会话授权已变化，请重新开始", 409)
        moment = now()
        consumed = await db.execute(update(VoiceSession).where(VoiceSession.id == item.id,
            VoiceSession.status == "pending", VoiceSession.ticket_hash == _hash(ticket),
            VoiceSession.ticket_consumed_at.is_(None), VoiceSession.owner_id.is_(None),
            VoiceSession.ticket_expires_at > moment, VoiceSession.pending_expires_at > moment).values(
                owner_id=owner_id, ticket_consumed_at=moment, status="connecting", started_at=moment,
                last_activity_at=moment, deadline_at=moment + timedelta(seconds=item.max_seconds),
                lease_until=moment + timedelta(seconds=runtime_settings().voice_lease_seconds))
            .execution_options(synchronize_session=False))
        if consumed.rowcount != 1:
            raise DomainError("voice_ticket_invalid", "语音连接凭据已被使用", 403)
        await db.refresh(item)
        await _changed(db, item, stream, "语音连接正在建立")
        scope = await _scope(db, item, user)
        messages = list(await db.scalars(select(Message).where(Message.conversation_id == item.conversation_id,
            Message.role.in_({"user", "assistant"})).order_by(Message.seq.desc()).limit(12)))
        context, chars = [], 0
        for message in reversed(messages):
            content = message.content[:max(0, 12000 - chars)]
            if content:
                context.append({"role": message.role, "content": content})
                chars += len(content)
        return {**scope, "context": context}


async def _owner(db, session_id, owner_id):
    item, user, stream = await _locked(db, session_id)
    if (not item.owner_id or not isinstance(owner_id, str) or not owner_id.isascii()
            or not secrets.compare_digest(item.owner_id, owner_id)):
        raise DomainError("voice_owner_mismatch", "语音连接所有者不符", 403)
    return item, user, stream


async def authorize_session(session_id, owner_id, auth_session_id=None):
    async with SessionFactory() as db, db.begin():
        item, user, stream = await _owner(db, session_id, owner_id)
        if auth_session_id is not None and auth_session_id != item.auth_session_id:
            raise DomainError("voice_owner_mismatch", "语音连接不属于当前登录", 403)
        return await _scope(db, item, user)


async def load_credentials(session_id, owner_id):
    async with SessionFactory() as db, db.begin():
        item, user, stream = await _owner(db, session_id, owner_id)
        if not (await _scope(db, item, user))["allowed"]:
            raise DomainError("voice_scope_closed", "语音授权已结束", 409)
        config = await db.get(VoiceConfig, item.config_id)
        secret = vault.open_secret(config.credential_ciphertext, config.credential_nonce, config.credential_key_id,
                                   config.id, config.provider, config.version)
        return {"provider": config.provider, "model_id": config.model_id, "voice": config.voice,
            "api_key": secret, "endpoint": config.endpoint, "api_version": config.api_version,
            "base_url": {"openai": "https://api.openai.com/v1", "google": "https://generativelanguage.googleapis.com",
                         "xai": "https://api.x.ai"}.get(config.provider)}


async def mark_connected(session_id, owner_id):
    async with SessionFactory() as db, db.begin():
        item, user, stream = await _owner(db, session_id, owner_id)
        scope = await _scope(db, item, user)
        if scope["allowed"]:
            item.status = "active"
            item.lease_until = now() + timedelta(seconds=runtime_settings().voice_lease_seconds)
            await _changed(db, item, stream, "语音会话已连接")
        return await _scope(db, item, user)


async def heartbeat(session_id, owner_id, activity=False):
    async with SessionFactory() as db, db.begin():
        item, user, stream = await _owner(db, session_id, owner_id)
        scope = await _scope(db, item, user)
        if scope["allowed"]:
            item.lease_until = now() + timedelta(seconds=runtime_settings().voice_lease_seconds)
            if activity:
                item.last_activity_at = now()
            item.updated_at = now()
        return scope


async def append_transcript(session_id, owner_id, transcript):
    index, speaker, content = transcript["index"], transcript["speaker"], transcript["text"]
    if (type(index) is not int or not 0 <= index < 1000000 or speaker not in {"user", "assistant"}
            or not isinstance(content, str) or len(content) > 12000 or "\x00" in content):
        raise DomainError("voice_transcript_invalid", "语音字幕数据超出限制", 422)
    try:
        content.encode("utf-8")
    except UnicodeError:
        raise DomainError("voice_transcript_invalid", "语音字幕编码无效", 422) from None
    async with SessionFactory() as db, db.begin():
        item, user, stream = await _owner(db, session_id, owner_id)
        row = await db.scalar(select(VoiceTurn).where(VoiceTurn.session_id == session_id,
            VoiceTurn.index == index, VoiceTurn.speaker == speaker))
        if item.cleanup_done:
            return None
        if row is None:
            count = await db.scalar(select(func.count()).select_from(VoiceTurn).where(VoiceTurn.session_id == session_id))
            if count >= 100:
                raise DomainError("voice_transcript_limit", "本次语音字幕条数已达上限", 429)
            row = VoiceTurn(session_id=item.id, user_id=user.id, index=index, speaker=speaker, text="")
            db.add(row)
        total = await db.scalar(select(func.sum(func.length(VoiceTurn.text))).where(VoiceTurn.session_id == session_id))
        if (total or 0) - len(row.text or "") + len(content) > 64000:
            raise DomainError("voice_transcript_limit", "本次语音字幕长度已达上限", 429)
        if row.final and not transcript.get("final"):
            return serialize(row, {"id", "session_id", "user_id", "created_at"})
        row.text, row.final = content, transcript.get("final") is True
        row.interrupted = transcript.get("interrupted") is True
        row.updated_at = now()
        item.last_activity_at = now()
        await _changed(db, item, stream, "语音字幕已更新，尚未作为正式消息发送")
        return serialize(row, {"id", "session_id", "user_id", "created_at"})


def _usage(item, incoming):
    if not isinstance(incoming, dict):
        return
    values = dict(item.usage or {})
    for field in ("requests", "input_tokens", "output_tokens", "total_tokens", "audio_input_tokens", "audio_output_tokens"):
        value = incoming.get(field)
        if type(value) is int and 0 <= value < 2 ** 63:
            values[field] = max(values.get(field, 0), value)
    values["total_tokens"] = max(values.get("total_tokens", 0), values.get("input_tokens", 0) + values.get("output_tokens", 0))
    if incoming.get("usage_complete") is True:
        values["usage_complete"] = True
    item.usage = values


async def record_usage(session_id, owner_id, usage):
    async with SessionFactory() as db, db.begin():
        item, user, stream = await _owner(db, session_id, owner_id)
        _usage(item, usage)


async def request_stop(user_id, session_id, key):
    async with SessionFactory() as db, db.begin():
        await owned(db, VoiceSession, session_id, user_id)
        item, user, stream = await _locked(db, session_id)
        if item.status not in FINAL and not item.stop_requested:
            item.stop_requested, item.reason = True, "stopped"
            if item.started_at is None:
                item.status, item.closed_at, item.cleanup_done = "closed", now(), True
                await _settle(db, item)
            else:
                item.status = "stopping"
            await _changed(db, item, stream, "已请求结束语音会话")
        return await public_session(db, item)


async def finish_session(session_id, owner_id, reason="closed", usage=None, cleanup_confirmed=True):
    reason = reason if reason in REASONS else "interrupted"
    async with SessionFactory() as db, db.begin():
        item, user, stream = await _owner(db, session_id, owner_id)
        _usage(item, usage)
        if item.status not in FINAL:
            effective = item.reason if item.stop_requested and item.reason else reason
            item.reason, item.error_message = effective, MESSAGES.get(effective)
            item.closed_at = now()
            item.status = ("closed" if effective in {"closed", "stopped"} else "expired" if effective == "duration_limit"
                           else "failed" if effective in {"provider_error", "protocol_error"} else "interrupted")
        item.stop_requested = True
        item.cleanup_done = item.cleanup_done or cleanup_confirmed is True
        await _settle(db, item, conservative=not item.cleanup_done)
        await _changed(db, item, stream, "语音会话已结束；未修改计划、提交或学习评估")
        return await public_session(db, item)
