"""User-started Coder runs. Generated files and command output are never learning evidence."""

import hashlib
import os
import tempfile
from datetime import timedelta
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, func, or_, select

from app.db.models import (Attachment, CodingArtifact, CodingCommand, CodingRun, Goal, Operation,
                           Outbox, Submission, Task, now)
from app.db.session import SessionFactory
from app.domain.common import DomainError, emit, lock_user, new_operation, owned, serialize, version
from app.domain.execution import operation_locked
from app.domain.model_usage import public_usage
from app.domain.sandbox_execution import _aware, storage_path
from app.integrations.settings import runtime_settings

ACTIVE = {"accepted", "running", "cancelling"}
FINAL = {"succeeded", "failed", "cancelled", "superseded", "unavailable", "expired"}
PROVIDER = "lazyx-workspace-v1"


async def public_run(db, item):
    value = serialize(item, {"user_id", "source", "workspace_id", "limits", "goal_revision", "plan_id",
                             "requirements_version", "workspace_open_attempted", "seed_saved", "export_saved", "cancel_requested"})
    op = await db.get(Operation, item.operation_id)
    value["usage"] = public_usage(op)
    rows = await db.scalars(select(CodingCommand).where(CodingCommand.run_id == item.id,
        CodingCommand.user_id == item.user_id).order_by(CodingCommand.created_at, CodingCommand.id).limit(100))
    value["commands"] = [serialize(row, {"user_id", "run_id", "remote_id", "created_at"}) for row in rows]
    rows = await db.scalars(select(CodingArtifact).where(CodingArtifact.run_id == item.id,
        CodingArtifact.user_id == item.user_id).order_by(CodingArtifact.filename))
    value["artifacts"] = [{"id": row.id, "filename": row.filename, "size": row.size,
        "sha256": row.sha256, "download_url": f"/api/v1/coding-runs/{item.id}/artifacts/{row.id}"} for row in rows]
    return value


def _state(item):
    return {"run_id": item.id, "operation_id": item.operation_id, "status": item.status, "stage": item.stage,
        "terminal": item.status in FINAL and (not item.workspace_open_attempted or item.cleanup_done),
        "workspace_created": item.workspace_created, "cleanup_done": item.cleanup_done,
        "workspace_open_attempted": item.workspace_open_attempted,
        "export_saved": item.export_saved, "cancel_requested": item.cancel_requested,
        "agent_ready": item.stage == "agent" and item.status == "running" and not item.cancel_requested,
        "remaining_seconds": max(0, (_aware(item.created_at) + timedelta(
            seconds=item.limits["deadline_seconds"]) - now()).total_seconds())}


async def _locked(db, operation_id):
    op, user, stream, goal = await operation_locked(db, operation_id)
    item = await db.scalar(select(CodingRun).where(CodingRun.operation_id == op.id,
                           CodingRun.user_id == op.user_id).with_for_update())
    if item is None or op.kind != "coding_run" or goal is None or item.goal_id != goal.id:
        raise DomainError("not_found", "编码任务不存在", 404)
    return item, op, user, stream, goal


async def _check(db, item, op, user, goal):
    if item.status in FINAL:
        # A later deadline, goal edit or cancellation cannot rewrite a completed run's
        # outcome while its independent workspace cleanup acknowledgement is pending.
        return False
    task = await db.get(Task, item.task_id)
    submission = await db.get(Submission, item.submission_id)
    valid = (user.active and goal.status == "active" and goal.control_revision == item.goal_revision
        and goal.active_plan_id == item.plan_id and task and task.plan_id == item.plan_id
        and task.active_submission_id == item.submission_id and task.requirements_version == item.requirements_version
        and task.status not in {"cancelled", "skipped"} and submission and submission.user_id == user.id)
    if not valid:
        item.cancel_requested = True
        item.status = "superseded"
        item.error_message = "目标、任务或保存的提交已变化，编码任务已停止"
    elif _state(item)["remaining_seconds"] <= 0:
        item.cancel_requested = True
        item.status, item.error_message = "expired", "本次编码任务已超过执行时限"
    elif op.status in {"cancelled", "superseded"}:
        item.cancel_requested = True
        if item.status in ACTIVE:
            item.status = "cancelling"
    return valid and not item.cancel_requested and item.status not in FINAL


async def _changed(db, item, op, stream, summary):
    item.updated_at = op.updated_at = now()
    op.stage = item.stage
    op.result = {"resource_id": item.id, "coding_run_id": item.id}
    op.status = ("running" if item.status in ACTIVE else item.status
                 if item.status in {"succeeded", "cancelled", "superseded"} else "failed")
    op.error_message = item.error_message
    await emit(db, stream, "coding.run_updated", item.id, summary, item.goal_id, op.id)


async def create_run(user_id, submission_id, payload, key):
    settings = runtime_settings()
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        if not user.active:
            raise DomainError("unauthenticated", "账号已停用", 401)
        submission = await owned(db, Submission, submission_id, user_id)
        task = await owned(db, Task, submission.task_id, user_id)
        goal = await owned(db, Goal, task.goal_id, user_id)
        op, created = await new_operation(db, user, stream, key, "coding_run",
                                         {**payload, "submission_id": submission_id}, goal)
        if not created:
            item = await db.scalar(select(CodingRun).where(CodingRun.operation_id == op.id))
            return await public_run(db, item)
        if not settings.coding_enabled:
            raise DomainError("coding_disabled", "编码助手尚未启用", 503)
        version(goal.control_revision, payload["expected_goal_revision"])
        version(task.state_version, payload["expected_version"])
        if (goal.status != "active" or task.plan_id != goal.active_plan_id
                or task.active_submission_id != submission.id or task.status in {"cancelled", "skipped"}
                or task.requirements_version != submission.requirements_version):
            raise DomainError("submission_changed", "只能从当前有效计划中最新保存的提交启动编码助手", 409)
        count = await db.scalar(select(func.count()).select_from(CodingRun).where(CodingRun.user_id == user_id,
            or_(CodingRun.status.in_(ACTIVE), and_(CodingRun.workspace_open_attempted.is_(True),
                                                 CodingRun.cleanup_done.is_(False)))))
        if count >= settings.coding_max_concurrent_per_user:
            raise DomainError("coding_busy", "已有编码任务正在运行或清理，请稍后重试", 429)
        count = await db.scalar(select(func.count()).select_from(CodingRun).where(CodingRun.user_id == user_id,
            CodingRun.created_at >= now() - timedelta(days=1)))
        if count >= settings.coding_max_daily_runs:
            raise DomainError("coding_budget_exceeded", "最近24小时的编码助手次数已用完", 429)
        source = submission.text
        attachment_id = payload.get("attachment_id")
        if attachment_id:
            if attachment_id not in submission.attachment_ids:
                raise DomainError("invalid_attachment", "附件不属于本次保存的提交", 422)
            attachment = await owned(db, Attachment, attachment_id, user_id)
            if not attachment.filename.lower().endswith(".py") or attachment.size > 1024 * 1024:
                raise DomainError("invalid_attachment", "请选择不超过1 MiB的Python附件", 422)
            with storage_path(attachment.storage_key).open("rb") as handle:
                content = handle.read(1024 * 1024 + 1)
            if len(content) != attachment.size or hashlib.sha256(content).hexdigest() != attachment.sha256:
                raise DomainError("attachment_changed", "附件校验失败", 422)
            try:
                source = content.decode("utf-8")
            except UnicodeError:
                raise DomainError("invalid_attachment", "Python附件必须使用UTF-8编码", 422) from None
        if not source.strip() or len(source.encode()) > 1024 * 1024:
            raise DomainError("invalid_code", "请先保存不超过1 MiB的Python代码", 422)
        instruction = payload["instruction"].strip()
        if not instruction or len(instruction) > 4000:
            raise DomainError("invalid_instruction", "请填写不超过4000字的编码任务说明", 422)
        item = CodingRun(user_id=user_id, goal_id=goal.id, task_id=task.id, submission_id=submission.id,
            operation_id=op.id, attachment_id=attachment_id, plan_id=task.plan_id,
            goal_revision=goal.control_revision, requirements_version=submission.requirements_version,
            instruction=instruction, source=source, workspace_id="coding_" + op.id.replace("-", ""),
            limits={"deadline_seconds": settings.coding_deadline_seconds, "memory_mb": settings.sandbox_memory_mb})
        db.add(item)
        await db.flush()
        op.result = {"resource_id": item.id, "coding_run_id": item.id}
        db.add(Outbox(user_id=user_id, goal_id=goal.id, operation_id=op.id, kind="temporal",
            dedupe_key=f"coding:{item.id}", payload={"type": "coding_run", "user_id": user_id,
                "goal_id": goal.id, "operation_id": op.id, "run_id": item.id}))
        await emit(db, stream, "coding.run_updated", item.id, "已排队启动编码助手", goal.id, op.id)
        return await public_run(db, item)


async def request_cancel(user_id, run_id, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        item = await owned(db, CodingRun, run_id, user_id)
        goal = await owned(db, Goal, item.goal_id, user_id)
        cmd, created = await new_operation(db, user, stream, key, "coding_cancel", {"run_id": run_id}, goal)
        if created:
            cmd.status, cmd.stage, cmd.result = "succeeded", "saved", {"resource_id": item.id}
            if not _state(item)["terminal"] and not item.cancel_requested:
                item.cancel_requested = True
                if item.status not in FINAL:
                    item.status = "cancelling"
                op = await db.get(Operation, item.operation_id)
                await _changed(db, item, op, stream, "已请求停止编码助手，正在清理工作区")
                db.add(Outbox(user_id=user_id, goal_id=goal.id, operation_id=op.id, kind="temporal",
                    dedupe_key=f"coding-cancel:{item.id}", payload={"type": "coding_run_cancelled",
                    "user_id": user_id, "goal_id": goal.id, "operation_id": op.id, "run_id": item.id}))
        return await public_run(db, item)


async def workspace_scope(operation_id, *, allow_finished=False):
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        allowed = await _check(db, item, op, user, goal)
        if not allowed and not allow_finished:
            await _changed(db, item, op, stream, "编码任务已停止，等待清理")
        result = {**_state(item), "allowed": allowed, "workspace_id": item.workspace_id,
                  "limits": dict(item.limits), "seed_saved": item.seed_saved}
        return result


async def check_model_scope(db, op, user, goal):
    """Called inside the existing model-budget transaction/lock; never opens another session."""
    item = await db.scalar(select(CodingRun).where(CodingRun.operation_id == op.id,
        CodingRun.user_id == user.id).with_for_update())
    if item is None or item.goal_id != goal.id:
        return False
    return await _check(db, item, op, user, goal)


async def record_command(operation_id, command_id, command, remote):
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        row = await db.scalar(select(CodingCommand).where(CodingCommand.run_id == item.id,
                                                         CodingCommand.remote_id == command_id))
        if row is not None and row.status in {"finished", "interrupted"} and remote["state"] != "finished":
            return  # A retried activity's initial placeholder cannot erase the real receipt.
        if row is not None and row.status == "finished" and row.exit_code is not None and remote["exit_code"] is None:
            return
        if row is None:
            count = await db.scalar(select(func.count()).select_from(CodingCommand).where(CodingCommand.run_id == item.id))
            if count >= 64:
                raise DomainError("command_budget", "本次编码命令次数达到上限", 429)
            row = CodingCommand(user_id=item.user_id, run_id=item.id, remote_id=command_id, command=command[:12000])
            db.add(row)
        row.status = "interrupted" if item.cleanup_done and remote["state"] != "finished" else remote["state"]
        row.stdout, row.stderr = remote["stdout"][:65536], remote["stderr"][:65536]
        row.exit_code = remote["exit_code"] if row.status == "finished" else None
        if row.status in {"finished", "interrupted"}:
            row.finished_at = now()
        await emit(db, stream, "coding.run_updated", item.id, "编码命令运行记录已更新", item.goal_id, op.id)


async def prepare_coding(operation_id):
    from app.integrations.workspace import SandboxWorkspaceBackend

    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        await _check(db, item, op, user, goal)
        state = _state(item)
        if item.status in FINAL or item.cancel_requested:
            return state
        workspace_id, source, attach = item.workspace_id, item.source, item.workspace_created
        item.workspace_open_attempted = True
        item.stage, item.status = "agent" if item.seed_saved else "opening", "running"
        await _changed(db, item, op, stream, "正在创建本次编码工作区")
    backend = SandboxWorkspaceBackend(operation_id=operation_id, workspace_id=workspace_id, attach=attach)
    await backend.working_dir()
    scope = await workspace_scope(operation_id)
    if scope["allowed"] and not scope["seed_saved"]:
        await backend.write_bytes("/workspace/submission.py", source.encode())
        async with SessionFactory() as db, db.begin():
            item, op, user, stream, goal = await _locked(db, operation_id)
            item.seed_saved = True
            item.stage = "agent"
            await _changed(db, item, op, stream, "编码助手正在处理保存的代码")
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        await _check(db, item, op, user, goal)
        return {**_state(item), "prompt": "保存的代码在 /workspace/submission.py。请完成以下用户本次指令：\n" + item.instruction,
            "deps": {"operation": {"id": op.id}, "coding_run_id": item.id,
                     "workspace_ref": {"provider": PROVIDER, "id": item.workspace_id}}}


async def workspace_opened(operation_id, workspace_id):
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        if item.workspace_id != workspace_id:
            raise DomainError("invalid_workspace", "工作区作用域不符", 409)
        item.workspace_created = True


def _save_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    prefix = f".{path.name}.pending-"
    # A killed worker can leave only this artifact's own temporary file. Its opaque path
    # is owned by this run and calls are serialized by the user's transaction lock.
    for stale in path.parent.glob(prefix + "*"):
        stale.unlink(missing_ok=True)
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=prefix, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
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


async def finish_coding(operation_id, summary="", *, error_code=None):
    from app.integrations.workspace import SandboxWorkspaceBackend

    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        await _check(db, item, op, user, goal)
        state = _state(item)
        if state["terminal"]:
            return state
        if summary and not item.summary:
            item.summary = summary[:16000]
        if error_code and not item.error_message:
            item.error_message = "编码助手未完成本次任务，请查看命令记录或稍后重试"
        if item.status not in FINAL:
            item.status = "cancelled" if item.cancel_requested else "failed" if error_code else "succeeded"
        item.stage = "exporting" if item.workspace_created and not item.export_saved else "closing"
        await _changed(db, item, op, stream, "正在保存生成文件并清理编码工作区")
        workspace_id = item.workspace_id
    backend = SandboxWorkspaceBackend(operation_id=operation_id, workspace_id=workspace_id, attach=True,
                                       allow_finished=True)
    # Native control-plane export, never an agent-authored manifest. Success commits before Close;
    # even storage/DB failures cannot leave an unbounded workspace alive.
    export_error = False
    try:
        if state["workspace_created"] and not state["export_saved"]:
            files = await backend.export_files()
            if (len(files) > 8 or len({filename for filename, _ in files}) != len(files)
                    or any(len(data) > 8 * 1024 * 1024 for _, data in files)
                    or sum(len(data) for _, data in files) > 16 * 1024 * 1024):
                raise ValueError("Export quota exceeded")
            async with SessionFactory() as db, db.begin():
                item, op, user, stream, goal = await _locked(db, operation_id)
                for filename, data in files:
                    name = PurePosixPath(filename)
                    if (name.is_absolute() or ".." in name.parts or not name.parts or str(name) != filename
                            or "\\" in filename or ":" in filename or len(filename) > 1024
                            or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
                        raise ValueError("Invalid export path")
                    identifier = str(uuid5(NAMESPACE_URL, f"coding:{item.id}:{filename}"))
                    old = await db.get(CodingArtifact, identifier)
                    if old:
                        continue
                    sha = hashlib.sha256(data).hexdigest()
                    key = f"coding-{item.id}-{identifier}"
                    _save_file(storage_path(key), data)
                    db.add(CodingArtifact(id=identifier, user_id=item.user_id, run_id=item.id, filename=filename,
                                         storage_key=key, size=len(data), sha256=sha))
                item.export_saved, item.stage = True, "closing"
                await _changed(db, item, op, stream, "生成文件已保存，正在销毁编码工作区")
    except Exception:
        export_error = True
    finally:
        if state["workspace_open_attempted"]:
            await backend.close()
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        if export_error:
            item.error_message = "部分生成文件无法导出；工作区已清理，已有命令记录仍可查看"
            if item.status == "succeeded":
                item.status = "failed"
        item.cleanup_done, item.stage = True, "finished"
        unfinished = await db.scalars(select(CodingCommand).where(CodingCommand.run_id == item.id,
            CodingCommand.user_id == item.user_id, CodingCommand.status.not_in({"finished", "interrupted"})))
        for command in unfinished:
            command.status, command.finished_at, command.exit_code = "interrupted", now(), None
        await _changed(db, item, op, stream, "编码任务已结束，未修改作业提交或学习评估")
        return _state(item)


async def cancel_coding(operation_id):
    async with SessionFactory() as db, db.begin():
        item, op, user, stream, goal = await _locked(db, operation_id)
        item.cancel_requested = True
    return await finish_coding(operation_id)


async def fail_coding(operation_id, code="agent_failed"):
    return await finish_coding(operation_id, error_code=code)
