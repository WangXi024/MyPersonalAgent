import hashlib
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import (
    BusinessEvent,
    Conversation,
    DailyBudget,
    EventStream,
    Goal,
    Message,
    Operation,
    Outbox,
    User,
    now,
)


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def serialize(entity, exclude=()) -> dict:
    values = {}
    for column in entity.__table__.columns:
        if column.name in exclude:
            continue
        value = getattr(entity, column.name)
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        values[column.name] = value.isoformat() if isinstance(value, (datetime, date)) else value
    return values


def public_user(user):
    return serialize(user, {"password_hash", "active", "memory_recall_enabled"})


def public_operation(op):
    return serialize(
        op, {"user_id", "idempotency_key", "request_hash", "payload", "versions", "agent_history"}
    )


async def lock_user(db, user_id):
    stream = await db.scalar(select(EventStream).where(EventStream.user_id == user_id).with_for_update())
    if stream is None:
        raise DomainError("not_found", "账号不存在", 404)
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    return user, stream


async def owned(db, model, entity_id, user_id):
    entity = await db.get(model, entity_id)
    if entity is None:
        raise DomainError("not_found", "资源不存在", 404)
    if hasattr(entity, "user_id"):
        valid = entity.user_id == user_id
    else:
        goal = await db.get(Goal, entity.goal_id)
        valid = goal is not None and goal.user_id == user_id
    if not valid:
        raise DomainError("not_found", "资源不存在", 404)
    return entity


def version(actual, expected):
    if actual != expected:
        raise DomainError("version_conflict", "内容已更新，请刷新后重试")


async def emit(db, stream, kind, entity_id, summary, goal_id=None, operation_id=None, entity_version=1):
    stream.last_seq += 1
    event = BusinessEvent(
        user_id=stream.user_id,
        seq=stream.last_seq,
        type=kind,
        entity_id=entity_id,
        summary=summary[:500],
        goal_id=goal_id,
        operation_id=operation_id,
        entity_version=entity_version,
    )
    db.add(event)
    await db.flush()
    return event


async def budget(db, user, local_date=None):
    local_date = local_date or now().astimezone(ZoneInfo(user.timezone)).date()
    row = await db.get(DailyBudget, (user.id, local_date))
    if row is None:
        row = DailyBudget(
            user_id=user.id,
            local_date=local_date,
            authorized_minutes=user.daily_minutes,
            consumed_minutes=0,
            reserved_minutes=0,
            version=1,
        )
        db.add(row)
        await db.flush()
    return row


async def reserve(db, user, day, minutes):
    row = await budget(db, user, day)
    if minutes > 0 and row.consumed_minutes + row.reserved_minutes + minutes > row.authorized_minutes:
        raise DomainError("budget_exceeded", "当天学习安排超过授权时间，请调整任务日期或预算")
    row.reserved_minutes = max(0, row.reserved_minutes + minutes)
    row.version += 1


async def new_operation(db, user, stream, key, kind, payload, goal=None, proactive=False):
    request_hash = digest({"kind": kind, "goal_id": goal.id if goal else None, "payload": payload})
    existing = await db.scalar(
        select(Operation).where(Operation.user_id == user.id, Operation.idempotency_key == key)
    )
    if existing:
        if existing.request_hash != request_hash:
            raise DomainError("idempotency_conflict", "幂等键已用于不同请求")
        return existing, False
    versions = {
        "availability_version": user.availability_version,
        "memory_control_revision": user.memory_control_revision,
        "control_revision": goal.control_revision if goal else None,
        "active_plan_id": goal.active_plan_id if goal else None,
    }
    if kind in {"tutor", "plan", "assessment", "recommendation"}:
        from app.integrations.model_catalog import resolve_model

        conversation = None
        if kind == "tutor" and payload.get("conversation_id"):
            conversation = await owned(db, Conversation, payload["conversation_id"], user.id)
        versions["model_alias"] = resolve_model(conversation.model_alias if conversation else None)
    op = Operation(
        user_id=user.id,
        goal_id=goal.id if goal else None,
        idempotency_key=key,
        request_hash=request_hash,
        kind=kind,
        payload=payload,
        versions=versions,
        proactive=proactive,
    )
    db.add(op)
    await db.flush()
    await emit(db, stream, "operation.accepted", op.id, "操作已接收", op.goal_id, op.id)
    return op, True


async def dispatch(db, operation, command):
    db.add(
        Outbox(
            user_id=operation.user_id,
            goal_id=operation.goal_id,
            operation_id=operation.id,
            kind="temporal",
            dedupe_key=f"temporal:{operation.id}",
            payload={
                "type": command,
                "user_id": operation.user_id,
                "goal_id": operation.goal_id,
                "operation_id": operation.id,
            },
        )
    )


async def message(db, stream, goal, op, content, role="assistant", kind="assistant_reply", task_id=None):
    conversation = (
        (await db.scalar(select(Conversation).where(Conversation.goal_id == goal.id)))
        if goal
        else (await db.get(Conversation, op.payload.get("conversation_id")))
    )
    if conversation is None or conversation.user_id != stream.user_id:
        raise DomainError("not_found", "对话不存在", 404)
    conversation.last_seq += 1
    msg = Message(
        conversation_id=conversation.id,
        operation_id=op.id if op else None,
        seq=conversation.last_seq,
        role=role,
        type=kind,
        content=content,
        task_id=task_id,
        proactive=bool(op and op.proactive),
        control_revision=goal.control_revision if goal else 0,
    )
    db.add(msg)
    await db.flush()
    await emit(
        db,
        stream,
        "message.created",
        msg.id,
        "有一条新消息",
        goal.id if goal else None,
        op.id if op else None,
    )
    return msg
