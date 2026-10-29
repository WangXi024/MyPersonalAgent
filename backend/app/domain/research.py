"""Operation-scoped research receipts, quotas and replay deduplication."""

from datetime import timedelta, timezone

from app.db.models import Conversation, now
from app.db.session import SessionFactory
from app.domain.common import DomainError, digest, emit
from app.domain.execution import TERMINAL, operation_locked
from app.integrations import research as web
from app.integrations.settings import runtime_settings


def _reply(status: str, message: str) -> dict:
    return {"status": status, "sources": [], "message": message}


async def _scope(db, operation_id):
    op, user, stream, goal = await operation_locked(db, operation_id)
    if op.kind != "tutor" or op.proactive or op.payload.get("research_requested") is not True:
        raise DomainError("permission_denied", "本次对话未开启联网，请开启联网后重新提问")
    if not user.active or op.status in TERMINAL:
        raise DomainError("operation_terminal", "本次任务已结束，未继续联网")
    created = op.created_at if op.created_at.tzinfo else op.created_at.replace(tzinfo=timezone.utc)
    if now() - created > timedelta(minutes=30):
        raise DomainError("operation_deadline", "本次任务已超时，未继续联网")
    conversation = await db.get(Conversation, op.payload.get("conversation_id"))
    if not conversation or conversation.user_id != user.id or conversation.goal_id != op.goal_id:
        raise DomainError("permission_denied", "本次对话不可用")
    if goal and (goal.user_id != user.id or goal.status in {"cancelled", "completed"}
                 or goal.control_revision != op.versions.get("control_revision")):
        raise DomainError("context_changed", "学习目标已变化，未继续联网")
    if user.memory_control_revision != op.versions.get("memory_control_revision"):
        raise DomainError("context_changed", "学习上下文已变化，未继续联网")
    return op, stream


async def _reserve(operation_id: str, kind: str, value: str, call_id: str) -> tuple[bool, dict]:
    settings = runtime_settings()
    if not settings.research_enabled:
        return False, _reply("disabled", "尚未启用联网资料服务")
    if not call_id or len(call_id) > 300:
        return False, _reply("invalid_request", "本次资料请求缺少有效标识")
    request_hash = digest({"kind": kind, "value": value})
    async with SessionFactory() as db, db.begin():
        op, stream = await _scope(db, operation_id)
        saved = dict(op.result or {})
        calls = list(saved.get("research_calls") or [])
        for call in calls:
            if call["id"] != call_id:
                continue
            if call["request_hash"] != request_hash:
                return False, _reply("invalid_request", "同一次资料请求不能更换参数")
            # Never repeat an outward request after a process loss in its uncertain window.
            return False, call.get("response") or _reply("unavailable", "该资料请求仍在处理或已中断，请重新提问")
        if len(calls) >= settings.research_max_calls:
            return False, _reply("budget_exceeded", "本次对话的联网次数已用完")
        calls.append({"id": call_id, "kind": kind, "request_hash": request_hash, "status": "pending"})
        op.result = {**saved, "research_calls": calls}
        await emit(db, stream, "operation.research_updated", op.id, "正在查找联网资料", op.goal_id, op.id)
    return True, {}


async def _finish(operation_id: str, call_id: str, response: dict) -> dict:
    async with SessionFactory() as db, db.begin():
        op, stream = await _scope(db, operation_id)
        saved = dict(op.result or {})
        calls = list(saved.get("research_calls") or [])
        sources = {source["id"]: source for source in saved.get("research_sources", [])}
        new_sources = response.get("sources", [])
        if response.get("source"):
            new_sources = [response["source"]]
        for source in new_sources:
            sources.setdefault(source["id"], source)
        for index, call in enumerate(calls):
            if call["id"] == call_id:
                calls[index] = {**call, "status": "done", "response": response}
                break
        op.result = {**saved, "research_calls": calls, "research_sources": list(sources.values())[:50]}
        await emit(db, stream, "operation.research_updated", op.id,
                   "联网资料已更新" if response["status"] == "ok" else "本次联网资料暂未获取成功",
                   op.goal_id, op.id)
        return response


async def _run(operation_id, kind, value, call_id, request):
    try:
        execute, cached = await _reserve(operation_id, kind, value, call_id)
        if not execute:
            return cached
        try:
            data = await request(value)
            response = {"status": "ok", "sources": data} if kind == "search" else {"status": "ok", **data}
        except web.ResearchError as error:
            response = _reply("unavailable", str(error))
        return await _finish(operation_id, call_id, response)
    except DomainError as error:
        return _reply(error.code, error.message)


async def search_for_operation(operation_id: str, query: str, tool_call_id: str) -> dict:
    """Send only a short, explicitly authorized search phrase, never agent context."""
    query = " ".join(str(query).split())
    if not query or len(query) > 300:
        return _reply("invalid_request", "请将搜索词控制在 1–300 个字符内，不要发送整段对话")
    return await _run(operation_id, "search", query, tool_call_id, web.search_web)


async def fetch_for_operation(operation_id: str, url: str, tool_call_id: str) -> dict:
    try:
        url = web.public_url(url, runtime_settings().research_allowed_domains)
    except web.ResearchError as error:
        return _reply("invalid_request", str(error))
    return await _run(operation_id, "fetch", url, tool_call_id, web.fetch_web)
