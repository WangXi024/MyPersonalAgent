"""Explicit saved-submission execution; never promotes an AI review or learning evidence."""

import hashlib
import os
import tempfile
from datetime import timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, func, or_, select

from app.config import get_settings
from app.db.models import (Attachment, Goal, Operation, Outbox, SandboxArtifact, SandboxExecution,
                           Submission, Task, now)
from app.db.session import SessionFactory
from app.domain.common import DomainError, emit, lock_user, new_operation, owned, serialize, version
from app.domain.execution import operation_locked
from app.integrations.sandbox import (SandboxClient, SandboxProtocolError, SandboxRPCError,
                                      execution_request, request_digest)
from app.integrations.settings import runtime_settings

ACTIVE = {"accepted", "running", "cancelling"}
FINAL = {"succeeded", "failed", "cancelled", "superseded", "unavailable", "expired"}


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _state(item):
    needs_ack = item.result_saved and not item.delivery_done
    terminal = item.status in FINAL and not needs_ack and (
        item.submitted_at is None or item.status in {"unavailable", "expired"} and not item.result_saved
        or item.result_saved and item.delivery_done and item.cleanup_done)
    return {"execution_id": item.id, "operation_id": item.operation_id, "job_id": item.request_id,
            "status": item.status, "phase": item.phase, "terminal": bool(terminal),
            "submitted": item.submitted_at is not None, "result_saved": item.result_saved,
            "needs_ack": needs_ack, "cleanup_done": item.cleanup_done,
            "delivery_done": item.delivery_done, "cancel_requested": item.cancel_requested,
            "remaining_seconds": max(0, ( _aware(item.created_at)
                + timedelta(seconds=item.limits["deadline_seconds"]) - now()).total_seconds())}


async def public_execution(db, item):
    value = serialize(item, {"user_id", "code", "limits", "request_hash", "goal_revision", "plan_id"})
    artifacts = await db.scalars(select(SandboxArtifact).where(SandboxArtifact.execution_id == item.id)
                                 .order_by(SandboxArtifact.created_at, SandboxArtifact.filename))
    value["artifacts"] = [{"id": artifact.id, "filename": artifact.filename,
                            "size": artifact.size, "sha256": artifact.sha256,
                            "download_url": f"/api/v1/sandbox-executions/{item.id}/artifacts/{artifact.id}"}
                           for artifact in artifacts]
    return value


def storage_path(key: str) -> Path:
    base = get_settings().attachment_dir.resolve()
    path = (base / key).resolve()
    if not path.is_relative_to(base) or path == base:
        raise DomainError("invalid_artifact", "产物存储路径无效", 500)
    return path


async def _locked(db, operation_id):
    op, user, stream, goal = await operation_locked(db, operation_id)
    item = await db.scalar(select(SandboxExecution).where(SandboxExecution.operation_id == op.id))
    if op.kind != "sandbox_execution" or item is None or item.user_id != user.id or item.goal_id != op.goal_id:
        raise DomainError("not_found", "执行记录不存在", 404)
    if not user.active and not item.result_saved:
        item.cancel_requested = True
        item.error_code, item.error_message = "ACCOUNT_INACTIVE", "账号已停用，正在停止代码执行"
    return item, op, user, stream, goal


async def _check_current(db, item, op, goal):
    task = await db.get(Task, item.task_id)
    submission = await db.get(Submission, item.submission_id)
    valid = (goal and goal.status == "active" and goal.user_id == item.user_id
             and goal.control_revision == item.goal_revision and goal.active_plan_id == item.plan_id
             and task and task.goal_id == goal.id and task.plan_id == item.plan_id
             and task.active_submission_id == item.submission_id
             and task.requirements_version == item.requirements_version
             and task.status not in {"cancelled", "skipped"}
             and submission and submission.user_id == item.user_id)
    if not valid and not item.result_saved:
        item.cancel_requested = True
        item.error_code, item.error_message = "CONTEXT_CHANGED", "目标、任务或提交已变化，本次执行不再生效"
    if op.status in {"cancelled", "superseded"} and not item.result_saved:
        item.cancel_requested = True
    if now() >= _aware(item.created_at) + timedelta(seconds=item.limits["deadline_seconds"]) and not item.result_saved:
        item.cancel_requested = True
        if not item.error_code:
            item.error_code, item.error_message = "DEADLINE_EXCEEDED", "本次代码任务已超时，未继续执行"


async def _changed(db, item, op, stream, summary):
    item.updated_at = now()
    if item.status in ACTIVE:
        if op.status not in {"cancelled", "superseded"}:
            op.status, op.stage = "running", "executing_code"
    else:
        op.status = item.status if item.status in {"succeeded", "cancelled", "superseded"} else "failed"
        op.stage = "saved" if item.result_saved else item.status
        op.error_code, op.error_message = item.error_code, item.error_message
    op.updated_at = now()
    op.result = {"resource_id": item.id, "execution_id": item.id}
    await emit(db, stream, "sandbox.execution_updated", item.id, summary, item.goal_id, op.id)


async def create_execution(user_id: str, submission_id: str, payload: dict, key: str):
    settings = runtime_settings()
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        if not user.active:
            raise DomainError("unauthenticated", "账号已停用", 401)
        submission = await owned(db, Submission, submission_id, user_id)
        task = await owned(db, Task, submission.task_id, user_id)
        goal = await owned(db, Goal, task.goal_id, user_id)
        op, created = await new_operation(db, user, stream, key, "sandbox_execution",
                                         {**payload, "submission_id": submission_id}, goal)
        if not created:
            item = await db.scalar(select(SandboxExecution).where(SandboxExecution.operation_id == op.id))
            return await public_execution(db, item)
        if not settings.sandbox_enabled:
            raise DomainError("sandbox_disabled", "代码运行服务尚未启用，已保存的提交不受影响", 503)
        if payload.get("language", "python") != "python":
            raise DomainError("invalid_language", "当前仅支持Python标准库代码", 422)
        version(goal.control_revision, payload["expected_goal_revision"])
        version(task.state_version, payload["expected_version"])
        if (goal.status != "active" or task.plan_id != goal.active_plan_id
                or task.active_submission_id != submission.id or task.status in {"cancelled", "skipped"}
                or task.requirements_version != submission.requirements_version):
            raise DomainError("submission_changed", "只能运行当前有效计划中最新保存的提交", 409)
        active = await db.scalar(select(func.count()).select_from(SandboxExecution).where(
            SandboxExecution.user_id == user_id, or_(SandboxExecution.status.in_(ACTIVE),
                and_(SandboxExecution.submitted_at.is_not(None), SandboxExecution.cleanup_done.is_(False)))))
        if active >= settings.sandbox_max_concurrent_per_user:
            raise DomainError("sandbox_busy", "已有代码任务正在执行，请等待或取消后重试", 429)
        recent = await db.scalar(select(func.count()).select_from(SandboxExecution).where(
            SandboxExecution.user_id == user_id, SandboxExecution.created_at >= now() - timedelta(days=1)))
        if recent >= settings.sandbox_max_daily_executions:
            raise DomainError("sandbox_budget_exceeded", "最近24小时的代码执行次数已用完", 429)
        attachment_id = payload.get("attachment_id")
        code = submission.text
        if attachment_id:
            if attachment_id not in submission.attachment_ids:
                raise DomainError("invalid_attachment", "附件不属于这次已保存的提交", 422)
            attachment = await owned(db, Attachment, attachment_id, user_id)
            if not attachment.filename.lower().endswith(".py") or attachment.size > 1024 * 1024:
                raise DomainError("invalid_attachment", "只能运行不超过1 MiB的Python附件", 422)
            with storage_path(attachment.storage_key).open("rb") as handle:
                content = handle.read(1024 * 1024 + 1)
            if len(content) != attachment.size or hashlib.sha256(content).hexdigest() != attachment.sha256:
                raise DomainError("attachment_changed", "附件校验失败，请重新上传", 422)
            try:
                code = content.decode("utf-8")
            except UnicodeError:
                raise DomainError("invalid_attachment", "Python附件必须使用UTF-8编码", 422) from None
        if not code.strip() or len(code.encode("utf-8")) > 1024 * 1024:
            raise DomainError("invalid_code", "请保存不超过1 MiB的Python代码后再运行", 422)
        request_id = "learning_" + op.id.replace("-", "")
        limits = {"memory_mb": settings.sandbox_memory_mb,
                  "timeout_seconds": settings.sandbox_execution_timeout_seconds,
                  "deadline_seconds": settings.sandbox_deadline_seconds}
        item = SandboxExecution(user_id=user_id, goal_id=goal.id, task_id=task.id,
            submission_id=submission.id, operation_id=op.id, attachment_id=attachment_id,
            request_id=request_id, request_hash=request_digest(execution_request(request_id, code, limits)),
            code=code, limits=limits, goal_revision=goal.control_revision, plan_id=task.plan_id,
            requirements_version=submission.requirements_version)
        db.add(item)
        await db.flush()
        op.result = {"resource_id": item.id, "execution_id": item.id}
        db.add(Outbox(user_id=user_id, goal_id=goal.id, operation_id=op.id, kind="temporal",
            dedupe_key=f"sandbox:{item.id}", payload={"type": "sandbox_execution", "user_id": user_id,
                "goal_id": goal.id, "operation_id": op.id, "execution_id": item.id}))
        await emit(db, stream, "sandbox.execution_updated", item.id, "已排队运行保存的Python代码", goal.id, op.id)
        return await public_execution(db, item)


async def request_cancel(user_id: str, execution_id: str, key: str):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        item = await owned(db, SandboxExecution, execution_id, user_id)
        goal = await owned(db, Goal, item.goal_id, user_id)
        command, created = await new_operation(db, user, stream, key, "sandbox_cancel",
                                             {"execution_id": execution_id}, goal)
        if created:
            command.status, command.stage, command.result = "succeeded", "saved", {"resource_id": item.id}
            if not _state(item)["terminal"] and not item.cancel_requested:
                item.cancel_requested = True
                if not item.result_saved:
                    item.status = "cancelling"
                op = await db.get(Operation, item.operation_id)
                await _changed(db, item, op, stream, "已请求停止代码执行，正在确认清理")
                db.add(Outbox(user_id=user_id, goal_id=goal.id, operation_id=op.id, kind="temporal",
                    dedupe_key=f"sandbox-cancel:{item.id}", payload={"type": "sandbox_execution_cancelled",
                        "user_id": user_id, "goal_id": goal.id, "operation_id": op.id, "execution_id": item.id}))
        return await public_execution(db, item)


async def prepare_execution(operation_id: str):
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        await _check_current(db, item, op, goal)
        if item.cancel_requested and item.submitted_at is None:
            item.status = ("failed" if item.error_code == "DEADLINE_EXCEEDED" else
                           "superseded" if item.error_code == "CONTEXT_CHANGED" else "cancelled")
            item.phase, item.outcome = "finished", "cancelled"
            item.cleanup_done = True  # No remote environment was submitted.
            await _changed(db, item, op, stream, "未启动的代码执行已取消")
        return _state(item)


async def _record_status(operation_id, remote):
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        await _check_current(db, item, op, goal)
        before = (item.phase, item.outcome, item.cleanup_done, item.delivery_done, item.status, item.error_code,
                  item.resource_usage)
        item.phase, item.outcome = remote["phase"], remote["outcome"]
        item.cleanup_done, item.delivery_done = remote["cleanup_done"], remote["delivery_done"]
        item.resource_usage = remote["resource_usage"]
        if item.error_code in {"DELIVERY_CONFIRMATION_PENDING", "CLEANUP_CONFIRMATION_PENDING"}:
            if item.delivery_done and item.cleanup_done:
                item.error_code = remote["error_code"]
                item.error_message = ("Python运行未正常完成，请查看运行日志；这不是学习评估结论"
                                      if item.status == "failed" else None)
        if not item.error_code:
            item.error_code = remote["error_code"]
        if not item.result_saved:
            item.status = "cancelling" if item.cancel_requested else "running"
        after = (item.phase, item.outcome, item.cleanup_done, item.delivery_done, item.status, item.error_code,
                 item.resource_usage)
        if before != after:
            await _changed(db, item, op, stream, "代码执行状态已更新")
        return _state(item)


async def submit_execution(operation_id: str):
    state = await prepare_execution(operation_id)
    if state["terminal"]:
        return state
    settings = runtime_settings()
    if not settings.sandbox_enabled:
        return await fail_execution(operation_id, "sandbox_disabled")
    async with SandboxClient() as client:
        if state["submitted"]:
            try:
                return await _record_status(operation_id, await client.status(state["job_id"]))
            except SandboxRPCError as error:
                if error.code != "NOT_FOUND":
                    raise
        ready = await client.ready()
        if not ready["ready"] or not ready["accepting_tasks"] or not ready["independent"]:
            raise SandboxRPCError("UNAVAILABLE", "NOT_READY")
        async with SessionFactory() as db, db.begin():
            item, op, user, stream, goal = await _locked(db, operation_id)
            await _check_current(db, item, op, goal)
            if item.cancel_requested:
                return _state(item)
            if item.submitted_at and now() - _aware(item.submitted_at) > timedelta(hours=23):
                raise SandboxRPCError("FAILED_PRECONDITION", "DEDUPLICATION_EXPIRED")
            request = execution_request(item.request_id, item.code, item.limits)
            if request_digest(request) != item.request_hash:
                raise SandboxProtocolError("Stored execution request changed")
            item.submitted_at = item.submitted_at or now()
            item.status = "running"
            await _changed(db, item, op, stream, "正在提交隔离执行任务")
        return await _record_status(operation_id, await client.submit(request))


async def poll_execution(operation_id: str):
    state = await prepare_execution(operation_id)
    if state["terminal"] or not state["submitted"]:
        return state
    async with SandboxClient() as client:
        try:
            return await _record_status(operation_id, await client.status(state["job_id"]))
        except SandboxRPCError as error:
            if error.code != "NOT_FOUND":
                raise
            return await fail_execution(operation_id, "job_not_found")


async def collect_execution(operation_id: str):
    state = await prepare_execution(operation_id)
    if state["result_saved"] or not state["submitted"]:
        return state
    async with SandboxClient() as client:
        try:
            result = await client.result(state["job_id"])
        except SandboxRPCError as error:
            if error.code != "NOT_FOUND":
                raise
            return await fail_execution(operation_id, "result_expired")
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        await _check_current(db, item, op, goal)
        if item.result_saved:
            return _state(item)
        for artifact in result["artifacts"]:
            artifact_id = str(uuid5(NAMESPACE_URL, f"{item.id}/{artifact['filename']}"))
            key = f"sandbox/{item.id}/{artifact_id}"
            path = storage_path(key)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Collection is serialized by the user's DB lock; no other collector can own
            # a pending file here. Remove debris from an earlier interrupted collection.
            for orphan in path.parent.glob(".pending-*"):
                if orphan.parent.resolve() == path.parent and not orphan.is_dir():
                    orphan.unlink(missing_ok=True)
            # Opaque names keep remote paths out of filesystem operations, including on Windows.
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                # Replaying a failed DB transaction may find a partial pre-existing file. Replace
                # it atomically from the validated RPC result; an interrupted temp file is never served.
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".pending-", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(artifact["data"])
                        handle.flush()
                        os.fsync(handle.fileno())
                    temporary.chmod(0o600)
                    os.replace(temporary, path)
                    if os.name != "nt":
                        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                finally:
                    if temporary:
                        temporary.unlink(missing_ok=True)
            db.add(SandboxArtifact(id=artifact_id, user_id=item.user_id, execution_id=item.id,
                filename=artifact["filename"], storage_key=key, size=artifact["size"], sha256=artifact["sha256"]))
        remote = result["status"]
        item.stdout, item.stderr = result["stdout"], result["stderr"]
        item.phase, item.outcome = remote["phase"], remote["outcome"]
        item.cleanup_done, item.delivery_done = remote["cleanup_done"], remote["delivery_done"]
        item.resource_usage, item.result_saved = remote["resource_usage"], True
        item.error_code = item.error_code or remote["error_code"]
        if item.error_code == "CONTEXT_CHANGED":
            item.status = "superseded"
        elif item.error_code == "DEADLINE_EXCEEDED":
            item.status = "failed"
        elif item.cancel_requested or item.outcome == "cancelled":
            item.status = "cancelled"
        else:
            item.status = {"success": "succeeded", "failed": "failed", "unknown": "unavailable"}[item.outcome]
        if item.status == "failed":
            item.error_message = "Python运行未正常完成，请查看运行日志；这不是学习评估结论"
        await _changed(db, item, op, stream, "代码运行回执已保存，未改变学习评估")
        return _state(item)


async def ack_execution(operation_id: str):
    state = await prepare_execution(operation_id)
    if not state["needs_ack"]:
        return state
    async with SandboxClient() as client:
        try:
            remote = await client.acknowledge(state["job_id"])
        except SandboxRPCError as error:
            if error.code != "NOT_FOUND":
                raise
            return await fail_execution(operation_id, "job_not_found")
    return await _record_status(operation_id, remote)


async def cancel_execution(operation_id: str):
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        item.cancel_requested = True
        state = _state(item)
    if not state["submitted"]:
        return await prepare_execution(operation_id)
    async with SandboxClient() as client:
        try:
            remote = await client.cancel(state["job_id"])
        except SandboxRPCError as error:
            if error.code != "NOT_FOUND":
                raise
            return await fail_execution(operation_id, "job_not_found")
    return await _record_status(operation_id, remote)


async def fail_execution(operation_id: str, code: str):
    errors = {"sandbox_disabled": "代码运行服务尚未启用", "result_expired": "运行结果已过期或因服务重启丢失，未自动重复执行",
              "job_not_found": "服务未找到原任务，执行结果不确定，未自动重复执行",
              "deadline_exceeded": "代码任务已超时，清理状态以服务回执为准"}
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        if item.result_saved:
            # A transport failure after collection cannot erase a real, durably saved outcome.
            if not item.delivery_done:
                item.error_code = "DELIVERY_CONFIRMATION_PENDING"
                item.error_message = "运行回执已保存，服务送达确认暂未完成；不会重新执行代码"
            elif not item.cleanup_done:
                item.error_code = "CLEANUP_CONFIRMATION_PENDING"
                item.error_message = "运行回执已保存，资源清理仍待服务确认"
            await _changed(db, item, op, stream, item.error_message or "运行回执已保存")
            return _state(item)
        item.status = "expired" if code == "result_expired" else "unavailable"
        item.error_code = code.upper() if code in errors else "SANDBOX_UNAVAILABLE"
        item.error_message = errors.get(code, "代码运行服务暂时不可用，执行与清理状态尚未确认")
        await _changed(db, item, op, stream, item.error_message)
        return _state(item)
