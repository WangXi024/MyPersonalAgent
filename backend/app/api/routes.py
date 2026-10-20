import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_, select

from app.agents.schemas import GoalDraft
from app.api.auth import current_user
from app.config import get_settings
from app.db.models import (
    Attachment,
    BusinessEvent,
    Conversation,
    DailyBudget,
    Evaluation,
    EventStream,
    Goal,
    MemorySource,
    Message,
    Milestone,
    Operation,
    Plan,
    SkillProgress,
    Submission,
    Task,
    now,
)
from app.db.session import SessionFactory
from app.domain import commands
from app.domain.common import (
    DomainError,
    dispatch,
    emit,
    lock_user,
    new_operation,
    owned,
    public_operation,
    public_user,
    serialize,
    version,
)

router = APIRouter()


def key(idempotency_key: str = Header(..., min_length=8, max_length=160)):
    return idempotency_key


class GoalInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    background: str = Field(default="", max_length=8000)
    outcome: str = Field(min_length=1, max_length=8000)
    criteria: list[str] = Field(default_factory=list, max_length=20)
    deadline: date | None = None


class VersionInput(BaseModel):
    expected_version: int = Field(ge=1)


class GoalEditInput(VersionInput):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    background: str | None = Field(default=None, max_length=8000)
    outcome: str | None = Field(default=None, min_length=1, max_length=8000)
    criteria: list[str] | None = Field(default=None, max_length=20)
    deadline: date | None = None


class InstructionInput(BaseModel):
    instruction: str = Field(default="", max_length=8000)


class DeferInput(VersionInput):
    scheduled_date: date


class StudyInput(BaseModel):
    attempt_id: str = Field(min_length=8, max_length=100)
    minutes: int = Field(ge=0, le=1440)
    estimated: bool = False


class SubmissionInput(VersionInput):
    text: str = Field(default="", max_length=100000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    attempt_id: str | None = Field(default=None, min_length=8, max_length=100)
    minutes: int | None = Field(default=None, ge=0, le=1440)
    estimated: bool = False

    @model_validator(mode="after")
    def content_required(self):
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError("提交需要文本或附件")
        return self


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=16000)
    task_id: str | None = None


class ReadInput(BaseModel):
    seq: int = Field(ge=0)


class SettingsInput(VersionInput):
    timezone: str | None = None
    daily_minutes: int | None = Field(default=None, ge=5, le=720)
    proactive_enabled: bool | None = None
    quiet_start: int | None = Field(default=None, ge=0, le=23)
    quiet_end: int | None = Field(default=None, ge=0, le=23)
    learning_hour: int | None = Field(default=None, ge=0, le=23)

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("时区无效")
        return value


class MemoryInput(BaseModel):
    content: str = Field(min_length=1, max_length=16000)


async def conversation_json(db, conversation):
    count = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
            Message.seq > conversation.read_seq,
        )
    )
    return {**serialize(conversation), "unread_count": count}


async def goal_json(db, goal):
    conversation = await db.scalar(select(Conversation).where(Conversation.goal_id == goal.id))
    item = serialize(goal)
    if conversation:
        item.update(
            conversation_id=conversation.id,
            unread_count=(await conversation_json(db, conversation))["unread_count"],
        )
    return item


@router.get("/bootstrap")
async def bootstrap(user=Depends(current_user)):
    async with SessionFactory() as db:
        if db.bind.dialect.name == "postgresql":
            await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        current = await db.get(type(user), user.id)
        stream = await db.get(EventStream, user.id)
        goals = list(
            (
                await db.scalars(select(Goal).where(Goal.user_id == user.id).order_by(Goal.created_at.desc()))
            ).all()
        )
        tasks = list(
            (
                await db.scalars(
                    select(Task)
                    .join(Goal)
                    .where(Goal.user_id == user.id, Task.status.notin_(["cancelled", "skipped", "completed"]))
                    .order_by(Task.scheduled_date)
                    .limit(100)
                )
            ).all()
        )
        operations = list(
            (
                await db.scalars(
                    select(Operation)
                    .where(Operation.user_id == user.id)
                    .order_by(Operation.created_at.desc())
                    .limit(30)
                )
            ).all()
        )
        events = list(
            (
                await db.scalars(
                    select(BusinessEvent)
                    .where(BusinessEvent.user_id == user.id)
                    .order_by(BusinessEvent.seq.desc())
                    .limit(40)
                )
            ).all()
        )
        conversations = list(
            (await db.scalars(select(Conversation).where(Conversation.user_id == user.id))).all()
        )
        day = now().astimezone(ZoneInfo(current.timezone)).date()
        row = await db.get(DailyBudget, (user.id, day))
        budget = (
            serialize(row)
            if row
            else {
                "local_date": day.isoformat(),
                "authorized_minutes": current.daily_minutes,
                "consumed_minutes": 0,
                "reserved_minutes": 0,
                "version": 1,
            }
        )
        return {
            "user": public_user(current),
            "goals": [await goal_json(db, g) for g in goals],
            "tasks": [serialize(t) for t in tasks],
            "task_counts": {
                "awaiting_review": await db.scalar(
                    select(func.count())
                    .select_from(Task)
                    .join(Goal)
                    .where(Goal.user_id == user.id, Task.status == "awaiting_review")
                )
            },
            "operations": [public_operation(op) for op in operations],
            "events": [serialize(e, {"user_id"}) for e in reversed(events)],
            "conversations": [await conversation_json(db, c) for c in conversations],
            "budget": budget,
            "cursor": stream.last_seq,
        }


@router.get("/goals")
async def goals(user=Depends(current_user)):
    async with SessionFactory() as db:
        items = await db.scalars(select(Goal).where(Goal.user_id == user.id).order_by(Goal.created_at.desc()))
        return {"items": [await goal_json(db, g) for g in items]}


@router.post("/goals", status_code=202)
async def create_goal(payload: GoalInput, user=Depends(current_user), request_key=Depends(key)):
    return await commands.create_goal(user.id, request_key, payload.model_dump(mode="json"))


@router.patch("/goals/{goal_id}", status_code=202)
async def edit_goal(
    goal_id: str, payload: GoalEditInput, user=Depends(current_user), request_key=Depends(key)
):
    changes = payload.model_dump(mode="json", exclude_unset=True, exclude={"expected_version"})
    if any(value is None for name, value in changes.items() if name != "deadline"):
        raise DomainError("invalid_field", "目标字段不能设为空值", 422)
    return await commands.edit_goal(user.id, goal_id, changes, payload.expected_version, request_key)


@router.get("/goals/{goal_id}")
async def goal_detail(goal_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        goal = await owned(db, Goal, goal_id, user.id)
        result = {"goal": await goal_json(db, goal)}
        for label, model in (
            ("plans", Plan),
            ("milestones", Milestone),
            ("tasks", Task),
            ("skills", SkillProgress),
        ):
            result[label] = [
                serialize(item) for item in await db.scalars(select(model).where(model.goal_id == goal_id))
            ]
        conversation = await db.scalar(select(Conversation).where(Conversation.goal_id == goal_id))
        result["conversation"] = await conversation_json(db, conversation)
        result["operations"] = [
            public_operation(item)
            for item in await db.scalars(
                select(Operation)
                .where(Operation.goal_id == goal_id)
                .order_by(Operation.created_at.desc())
                .limit(40)
            )
        ]
        result["submissions"] = [
            serialize(item)
            for item in await db.scalars(
                select(Submission)
                .join(Task)
                .where(Task.goal_id == goal_id)
                .order_by(Submission.created_at.desc())
                .limit(100)
            )
        ]
        result["evaluations"] = [
            serialize(item)
            for item in await db.scalars(
                select(Evaluation).join(Submission).join(Task).where(Task.goal_id == goal_id)
            )
        ]
        return result


@router.post("/goals/{goal_id}/plan-proposals", status_code=202)
async def plan_proposals(
    goal_id: str, payload: InstructionInput, user=Depends(current_user), request_key=Depends(key)
):
    return await commands.request_operation(user.id, request_key, goal_id, "plan", payload.model_dump())


@router.post("/goals/{goal_id}/recommendations", status_code=202)
async def recommendations(
    goal_id: str, payload: InstructionInput, user=Depends(current_user), request_key=Depends(key)
):
    return await commands.request_operation(
        user.id, request_key, goal_id, "recommendation", payload.model_dump()
    )


@router.post("/plans/{plan_id}/{action}")
async def plan_action(
    plan_id: str, action: str, payload: VersionInput, user=Depends(current_user), request_key=Depends(key)
):
    if action not in {"accept", "reject"}:
        raise DomainError("not_found", "操作不存在", 404)
    return await commands.accept_plan(
        user.id, plan_id, payload.expected_version, action == "accept", request_key
    )


@router.post("/goals/{goal_id}/{action}")
async def goal_action(
    goal_id: str, action: str, payload: VersionInput, user=Depends(current_user), request_key=Depends(key)
):
    if action not in {"pause", "resume", "cancel", "complete"}:
        raise DomainError("not_found", "操作不存在", 404)
    return await commands.control_goal(user.id, goal_id, action, payload.expected_version, request_key)


@router.post("/tasks/{task_id}/study-records")
async def study_record(task_id: str, payload: StudyInput, user=Depends(current_user)):
    return await commands.study_record(user.id, task_id, payload.model_dump())


@router.post("/tasks/{task_id}/submissions", status_code=202)
async def submission(
    task_id: str, payload: SubmissionInput, user=Depends(current_user), request_key=Depends(key)
):
    return await commands.submit(user.id, task_id, payload.model_dump(), request_key)


@router.post("/tasks/{task_id}/defer")
async def defer(task_id: str, payload: DeferInput, user=Depends(current_user), request_key=Depends(key)):
    return await commands.task_action(user.id, task_id, "defer", payload.model_dump(mode="json"), request_key)


@router.post("/tasks/{task_id}/{action}")
async def task_action(
    task_id: str, action: str, payload: VersionInput, user=Depends(current_user), request_key=Depends(key)
):
    if action not in {"start", "skip"}:
        raise DomainError("not_found", "操作不存在", 404)
    return await commands.task_action(user.id, task_id, action, payload.model_dump(), request_key)


@router.get("/submissions/{submission_id}/evaluation")
async def evaluation(submission_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        submission = await owned(db, Submission, submission_id, user.id)
        evaluations = await db.scalars(select(Evaluation).where(Evaluation.submission_id == submission.id))
        return {"submission": serialize(submission), "evaluations": [serialize(e) for e in evaluations]}


@router.post("/assistant/conversation")
async def assistant_conversation(user=Depends(current_user)):
    async with SessionFactory() as db, db.begin():
        current, stream = await lock_user(db, user.id)
        conversation = await db.scalar(
            select(Conversation).where(Conversation.user_id == user.id, Conversation.goal_id.is_(None))
        )
        if conversation is None:
            conversation = Conversation(user_id=user.id, goal_id=None)
            db.add(conversation)
            await db.flush()
            await emit(db, stream, "conversation.created", conversation.id, "已建立与学习助手的对话")
        return await conversation_json(db, conversation)


@router.get("/conversations/{conversation_id}/messages")
async def messages(
    conversation_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    user=Depends(current_user),
):
    async with SessionFactory() as db:
        conversation = await owned(db, Conversation, conversation_id, user.id)
        items = list(
            (
                await db.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id, Message.seq > after)
                    .order_by(Message.seq)
                    .limit(limit)
                )
            ).all()
        )
        return {
            "items": [serialize(item) for item in items],
            "last_seq": conversation.last_seq,
            "read_seq": conversation.read_seq,
            "has_more": bool(items and items[-1].seq < conversation.last_seq),
        }


@router.get("/conversations/{conversation_id}/goal-drafts")
async def goal_drafts(conversation_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        conversation = await owned(db, Conversation, conversation_id, user.id)
        return {"items": await commands.goal_draft_items(db, conversation)}


@router.post("/goal-drafts/{operation_id}/accept", status_code=202)
async def accept_goal_draft(
    operation_id: str, payload: GoalDraft, user=Depends(current_user), request_key=Depends(key)
):
    return await commands.accept_goal_draft(
        user.id, operation_id, request_key, payload.model_dump(mode="json")
    )


@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def send_message(
    conversation_id: str, payload: MessageInput, user=Depends(current_user), request_key=Depends(key)
):
    return await commands.send_message(user.id, conversation_id, payload.model_dump(), request_key)


@router.post("/conversations/{conversation_id}/read")
async def read(conversation_id: str, payload: ReadInput, user=Depends(current_user)):
    async with SessionFactory() as db, db.begin():
        current, stream = await lock_user(db, user.id)
        conversation = await owned(db, Conversation, conversation_id, user.id)
        conversation.read_seq = max(conversation.read_seq, min(payload.seq, conversation.last_seq))
        await emit(db, stream, "conversation.read", conversation.id, "已读状态已同步", conversation.goal_id)
        return await conversation_json(db, conversation)


@router.get("/operations/{operation_id}")
async def operation(operation_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        return public_operation(await owned(db, Operation, operation_id, user.id))


@router.post("/operations/{operation_id}/cancel")
async def cancel_operation(operation_id: str, user=Depends(current_user)):
    async with SessionFactory() as db, db.begin():
        current, stream = await lock_user(db, user.id)
        op = await owned(db, Operation, operation_id, user.id)
        if op.status not in {"succeeded", "failed", "superseded", "cancelled"}:
            op.status, op.stage = "cancelled", "cancelled"
            from app.db.models import Outbox

            db.add(
                Outbox(
                    user_id=user.id,
                    goal_id=op.goal_id,
                    operation_id=op.id,
                    kind="temporal",
                    dedupe_key=f"operation-cancel:{op.id}",
                    payload={
                        "type": "operation_cancelled",
                        "user_id": user.id,
                        "goal_id": op.goal_id,
                        "operation_id": op.id,
                    },
                )
            )
            await emit(
                db, stream, "operation.cancelled", op.id, "操作已取消，迟到结果不会发布", op.goal_id, op.id
            )
        return public_operation(op)


@router.post("/operations/{operation_id}/retry", status_code=202)
async def retry_operation(operation_id: str, user=Depends(current_user), request_key=Depends(key)):
    async with SessionFactory() as db, db.begin():
        current, stream = await lock_user(db, user.id)
        op = await owned(db, Operation, operation_id, user.id)
        if op.status not in {"failed", "superseded"} or op.kind not in {
            "plan",
            "tutor",
            "assessment",
            "recommendation",
        }:
            raise DomainError("invalid_transition", "该操作当前不能重试")
        goal = await owned(db, Goal, op.goal_id, user.id) if op.goal_id else None
        if (
            goal
            and (
                goal.status in {"cancelled", "completed"} or (goal.status == "paused" and op.kind != "tutor")
            )
        ) or (goal is None and op.kind != "tutor"):
            raise DomainError("goal_inactive", "请先恢复目标")
        new, created = await new_operation(
            db, current, stream, request_key, op.kind, op.payload, goal, op.proactive
        )
        if created:
            await dispatch(
                db,
                new,
                {
                    "plan": "goal_created",
                    "tutor": "user_message" if goal else "assistant_message",
                    "assessment": "submission_created",
                    "recommendation": "recommendation_requested",
                }[op.kind],
            )
        return commands.accepted(new)


@router.get("/settings")
async def settings(user=Depends(current_user)):
    return public_user(user)


@router.patch("/settings")
async def update_settings(payload: SettingsInput, user=Depends(current_user)):
    async with SessionFactory() as db, db.begin():
        current, stream = await lock_user(db, user.id)
        version(current.availability_version, payload.expected_version)
        changes = payload.model_dump(exclude_none=True, exclude={"expected_version"})
        for name, value in changes.items():
            setattr(current, name, value)
        current.availability_version += 1
        if "daily_minutes" in changes or "timezone" in changes:
            today = now().astimezone(ZoneInfo(current.timezone)).date()
            budgets = await db.scalars(
                select(DailyBudget).where(DailyBudget.user_id == user.id, DailyBudget.local_date >= today)
            )
            for row in budgets:
                row.authorized_minutes = current.daily_minutes
                row.version += 1
            # Free future unstarted reservations; a new confirmed plan is required to add them back.
            tasks = await db.scalars(
                select(Task)
                .join(Goal)
                .where(
                    Goal.user_id == user.id,
                    Task.scheduled_date >= today,
                    Task.status.in_(["ready", "needs_revision"]),
                )
            )
            from app.domain.common import reserve

            for task in tasks:
                if task.reserved_minutes:
                    await reserve(db, current, task.scheduled_date, -task.reserved_minutes)
                    task.reserved_minutes = 0
                    task.status = "cancelled"
                    task.state_version += 1
            goals = await db.scalars(select(Goal).where(Goal.user_id == user.id, Goal.status == "active"))
            for goal in goals:
                op, created = await new_operation(
                    db,
                    current,
                    stream,
                    f"settings:{current.availability_version}:{goal.id}",
                    "plan",
                    {"instruction": "可用时间或偏好变化，请重新安排近期任务"},
                    goal,
                )
                if created:
                    await dispatch(db, op, "availability_changed")
        await emit(
            db,
            stream,
            "settings.updated",
            user.id,
            "学习设置已更新",
            entity_version=current.availability_version,
        )
        return public_user(current)


@router.get("/memories")
async def memories(user=Depends(current_user)):
    async with SessionFactory() as db:
        items = await db.scalars(
            select(MemorySource)
            .where(
                MemorySource.user_id == user.id,
                or_(MemorySource.deleted.is_(False), MemorySource.status == "failed"),
            )
            .order_by(MemorySource.created_at.desc())
        )
        return {
            "items": [
                {
                    **serialize(item, {"user_id", "external_id"}),
                    "content": "已删除的记忆仍在等待外部清理，可重试同步。" if item.deleted else item.content,
                }
                for item in items
            ]
        }


@router.patch("/memories/{memory_id}")
async def memory_update(memory_id: str, payload: MemoryInput, user=Depends(current_user)):
    return await commands.change_memory(user.id, memory_id, payload.content)


@router.delete("/memories/{memory_id}")
async def memory_delete(memory_id: str, user=Depends(current_user)):
    return await commands.change_memory(user.id, memory_id)


@router.post("/attachments")
async def upload(file: UploadFile = File(...), user=Depends(current_user), request_key=Depends(key)):
    filename = Path(file.filename or "attachment").name[:255]
    extension = Path(filename).suffix.lower()
    if extension not in {".txt", ".md", ".pdf", ".py", ".js", ".ts", ".json", ".csv"}:
        raise DomainError("invalid_file_type", "仅支持文本、Markdown、PDF 和代码文本文件", 422)
    data = await file.read(get_settings().max_upload_bytes + 1)
    if len(data) > get_settings().max_upload_bytes:
        raise DomainError("file_too_large", "文件超过 10 MiB 限制", 413)
    if extension == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise DomainError("invalid_file", "PDF 文件格式无效", 422)
        mime = "application/pdf"
    else:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise DomainError("invalid_file", "文本文件必须使用 UTF-8 编码", 422)
        mime = "text/plain"
    storage_key = str(uuid4())
    directory = get_settings().attachment_dir
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / storage_key
    path.write_bytes(data)
    path.chmod(0o600)
    try:
        async with SessionFactory() as db, db.begin():
            current, stream = await lock_user(db, user.id)
            op, created = await new_operation(
                db,
                current,
                stream,
                request_key,
                "attachment",
                {
                    "filename": filename,
                    "mime": mime,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
            if not created:
                attachment = await owned(db, Attachment, op.result["resource_id"], user.id)
                path.unlink(missing_ok=True)
                return serialize(attachment, {"user_id", "storage_key"})
            attachment = Attachment(
                user_id=user.id,
                storage_key=storage_key,
                filename=filename,
                mime=mime,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            db.add(attachment)
            await db.flush()
            op.status, op.stage, op.result = "succeeded", "saved", {"resource_id": attachment.id}
            await emit(db, stream, "attachment.stored", attachment.id, "附件已安全保存", operation_id=op.id)
            return serialize(attachment, {"user_id", "storage_key"})
    except Exception:
        path.unlink(missing_ok=True)
        raise


@router.get("/attachments/{attachment_id}/download")
async def download(attachment_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        attachment = await owned(db, Attachment, attachment_id, user.id)
        path = get_settings().attachment_dir / attachment.storage_key
        if not path.is_file():
            raise DomainError("file_missing", "附件暂不可用", 503)
        return FileResponse(
            path,
            filename=attachment.filename,
            media_type=attachment.mime,
            headers={"X-Content-Type-Options": "nosniff"},
        )


@router.get("/events")
async def events(request: Request, cursor: int = Query(0, ge=0), user=Depends(current_user)):
    try:
        cursor = int(request.headers.get("last-event-id", cursor))
    except ValueError:
        raise DomainError("invalid_cursor", "事件游标无效", 422)

    async def stream():
        nonlocal cursor
        count = 0
        while not await request.is_disconnected():
            # Recheck expiration/revocation so a long-lived stream cannot outlive logout.
            try:
                await current_user(request)
            except DomainError:
                yield "event: session_expired\ndata: {}\n\n"
                return
            async with SessionFactory() as db:
                minimum = await db.scalar(
                    select(func.min(BusinessEvent.seq)).where(BusinessEvent.user_id == user.id)
                )
                watermark = await db.get(EventStream, user.id)
                if (
                    cursor > watermark.last_seq
                    or (minimum is not None and cursor < minimum - 1)
                    or (minimum is None and cursor < watermark.last_seq)
                ):
                    yield "event: reset_required\ndata: {}\n\n"
                    return
                rows = list(
                    (
                        await db.scalars(
                            select(BusinessEvent)
                            .where(BusinessEvent.user_id == user.id, BusinessEvent.seq > cursor)
                            .order_by(BusinessEvent.seq)
                            .limit(100)
                        )
                    ).all()
                )
            for item in rows:
                cursor = item.seq
                yield f"id: {item.seq}\nevent: business\ndata: {json.dumps(serialize(item, {'user_id'}), ensure_ascii=False)}\n\n"
            count += 1
            if count % 15 == 0:
                yield ": heartbeat\n\n"
            await asyncio.sleep(get_settings().event_poll_seconds)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/memories/{memory_id}/retry")
async def memory_retry(memory_id: str, user=Depends(current_user), request_key=Depends(key)):
    from app.db.models import Outbox

    async with SessionFactory() as db, db.begin():
        current, stream = await lock_user(db, user.id)
        source = await owned(db, MemorySource, memory_id, user.id)
        if source.status != "failed":
            raise DomainError("invalid_transition", "该记忆当前无需重试")
        source.status = "pending"
        db.add(
            Outbox(
                user_id=user.id,
                kind="memory",
                dedupe_key=f"memory-retry:{source.id}:{request_key}",
                payload={
                    "type": "memory_sync",
                    "user_id": user.id,
                    "source_id": source.id,
                    "version": source.version,
                    "action": "delete" if source.suppressed else "retain",
                },
            )
        )
        await emit(db, stream, "memory.sync_changed", source.id, "记忆同步已重新排队")
        return {"status": "pending"}
