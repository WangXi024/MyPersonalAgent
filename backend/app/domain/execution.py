"""Database boundaries used by durable Activities; no network I/O while holding locks."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select

from app.agents.schemas import GoalDraft
from app.config import get_settings
from app.db.models import (
    Attachment,
    DailyBudget,
    Conversation,
    Evaluation,
    Goal,
    MemorySource,
    Message,
    Milestone,
    Operation,
    Outbox,
    Plan,
    SkillProgress,
    Submission,
    Task,
    User,
    now,
)
from app.db.session import SessionFactory
from app.domain.common import (
    DomainError,
    budget,
    emit,
    lock_user,
    message,
    new_operation,
    dispatch,
    public_operation,
    public_user,
    reserve,
    serialize,
)

TERMINAL = {"succeeded", "failed", "superseded", "cancelled"}


class TaskCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=16000)
    criteria: list[str] = Field(min_length=1, max_length=20)
    skills: list[str] = Field(default_factory=list, max_length=20)
    estimated_minutes: int = Field(ge=1, le=240)
    scheduled_date: date
    milestone_index: int = Field(default=0, ge=0)
    review_of_task_id: str | None = None


class MilestoneCandidate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    criteria: list[str] = Field(min_length=1, max_length=20)


class PlanCandidate(BaseModel):
    milestones: list[MilestoneCandidate] = Field(min_length=1, max_length=20)
    tasks: list[TaskCandidate] = Field(min_length=1, max_length=40)
    summary: str = Field(min_length=1, max_length=16000)
    message: str = Field(default="计划已准备好，请查看并确认。", max_length=16000)


async def insert_task(db, user, goal, plan, item, milestone_id=None):
    candidate = TaskCandidate.model_validate(item)
    today = now().astimezone(ZoneInfo(user.timezone)).date()
    if candidate.scheduled_date < today:
        raise DomainError("stale_schedule", "任务日期已过期，需要重新规划")
    if candidate.review_of_task_id:
        source = await db.get(Task, candidate.review_of_task_id)
        if source is None or source.goal_id != goal.id or source.status in {"cancelled", "skipped"}:
            raise DomainError("invalid_review_source", "复习来源任务不属于当前目标或已失效", 422)
        evidence = await db.scalar(
            select(Evaluation.id)
            .join(Submission)
            .where(
                Submission.task_id == source.id,
                Evaluation.valid.is_(True),
                Evaluation.rubric_version == source.requirements_version,
            )
            .limit(1)
        )
        if evidence is None:
            raise DomainError("invalid_review_source", "复习来源任务尚无有效评估依据", 422)
    await reserve(db, user, candidate.scheduled_date, candidate.estimated_minutes)
    task = Task(
        goal_id=goal.id,
        plan_id=plan.id,
        milestone_id=milestone_id,
        **candidate.model_dump(exclude={"milestone_index"}),
        reserved_minutes=candidate.estimated_minutes,
    )
    db.add(task)
    await db.flush()
    return task


async def check_plan_cap(db, user, goal, proposal: dict):
    cap = proposal.get("remaining_today_minutes")
    if cap is None:
        return
    cap_day = date.fromisoformat(proposal["cap_date"])
    row = await budget(db, user, cap_day)
    replaced = await db.scalar(
        select(func.coalesce(func.sum(Task.reserved_minutes), 0)).where(
            Task.goal_id == goal.id,
            Task.scheduled_date == cap_day,
            Task.status.in_(["ready", "needs_revision"]),
        )
    )
    proposed = sum(
        item["estimated_minutes"] for item in proposal["tasks"] if item["scheduled_date"] == str(cap_day)
    )
    if row.reserved_minutes - replaced + proposed > cap:
        raise DomainError(
            "remaining_time_exceeded", "候选计划超过今天剩余可安排时间，请重新规划或先调整其他目标"
        )


async def load_context(operation_id: str) -> dict:
    async with SessionFactory() as db:
        op = await db.get(Operation, operation_id)
        if op is None:
            raise DomainError("not_found", "操作不存在", 404)
        user = await db.get(User, op.user_id)
        goal = await db.get(Goal, op.goal_id) if op.goal_id else None
        conversation = (
            (
                await db.scalar(
                    select(Conversation).where(
                        Conversation.goal_id == op.goal_id, Conversation.user_id == op.user_id
                    )
                )
            )
            if goal
            else (
                await db.scalar(
                    select(Conversation).where(
                        Conversation.id == op.payload.get("conversation_id"),
                        Conversation.user_id == op.user_id,
                        Conversation.goal_id.is_(None),
                    )
                )
            )
        )
        messages = (
            list(
                (
                    await db.scalars(
                        select(Message)
                        .where(
                            Message.conversation_id == conversation.id,
                            or_(
                                Message.role != "user",
                                Message.seq <= op.payload.get("message_seq", conversation.last_seq),
                            ),
                        )
                        .order_by(Message.seq.desc())
                        .limit(30)
                    )
                ).all()
            )
            if conversation
            else []
        )
        memories = list(
            (
                await db.scalars(
                    select(MemorySource)
                    .where(
                        MemorySource.user_id == user.id,
                        MemorySource.suppressed.is_(False),
                        MemorySource.deleted.is_(False),
                    )
                    .order_by(MemorySource.created_at.desc())
                    .limit(30)
                )
            ).all()
        )
        tasks = (
            list(
                (
                    await db.scalars(
                        select(Task)
                        .where(
                            Task.goal_id == goal.id,
                            Task.plan_id == goal.active_plan_id,
                            Task.status != "cancelled",
                        )
                        .order_by(Task.scheduled_date)
                        .limit(40)
                    )
                ).all()
            )
            if goal
            else []
        )
        submission = (
            await db.get(Submission, op.payload["submission_id"]) if op.payload.get("submission_id") else None
        )
        plan = await db.get(Plan, goal.active_plan_id) if goal and goal.active_plan_id else None
        progress = (
            list(
                (
                    await db.scalars(
                        select(SkillProgress)
                        .where(SkillProgress.goal_id == goal.id)
                        .order_by(SkillProgress.review_at)
                        .limit(80)
                    )
                ).all()
            )
            if goal
            else []
        )
        evidence = (
            list(
                (
                    await db.scalars(
                        select(Evaluation)
                        .join(Submission)
                        .join(Task)
                        .where(Task.goal_id == goal.id, Evaluation.valid.is_(True))
                        .order_by(Evaluation.created_at.desc())
                        .limit(10)
                    )
                ).all()
            )
            if goal
            else []
        )
        today = now().astimezone(ZoneInfo(user.timezone)).date()
        budgets = []
        for offset in range(14):
            day = today + timedelta(days=offset)
            row = await db.get(DailyBudget, (user.id, day))
            value = (
                serialize(row)
                if row
                else {
                    "local_date": day.isoformat(),
                    "authorized_minutes": user.daily_minutes,
                    "consumed_minutes": 0,
                    "reserved_minutes": 0,
                }
            )
            value["available_minutes"] = max(
                0, value["authorized_minutes"] - value["consumed_minutes"] - value["reserved_minutes"]
            )
            budgets.append(value)
        attachments = []
        if submission:
            from app.domain.files import attachment_context

            for attachment_id in submission.attachment_ids:
                attachment = await db.get(Attachment, attachment_id)
                if attachment and attachment.user_id == user.id:
                    attachments.append(await attachment_context(attachment))
        message_data, message_chars = [], 0
        for item in messages:
            if message_chars + len(item.content) > 60000:
                break
            message_data.append(serialize(item))
            message_chars += len(item.content)
        task_data, task_chars = [], 0
        for item in tasks:
            data = serialize(item)
            data["instructions"] = data["instructions"][: max(0, 80000 - task_chars)]
            task_chars += len(data["instructions"])
            task_data.append(data)
        attachment_chars = 0
        for item in attachments:
            value = item.get("text", "")
            remaining = max(0, 64000 - attachment_chars)
            item["text"] = value[:remaining]
            item["truncated"] = item.get("truncated", False) or len(value) > remaining
            attachment_chars += len(item["text"])
        plan_data = serialize(plan, {"proposal"}) if plan else None
        if plan_data:
            plan_data["milestones"] = [
                {"title": m.title, "criteria": m.criteria, "status": m.status}
                for m in await db.scalars(select(Milestone).where(Milestone.plan_id == plan.id))
            ]
        for item in budgets:
            item["replaceable_minutes"] = sum(
                t.reserved_minutes
                for t in tasks
                if str(t.scheduled_date) == item["local_date"] and t.status in {"ready", "needs_revision"}
            )
        draft_states = []
        if goal is None and conversation:
            from app.domain.commands import goal_draft_items

            draft_states = [
                {
                    "operation_id": item["operation_id"],
                    "status": item["status"],
                    "title": item["draft"]["title"],
                    "goal_id": item["goal_id"],
                    "plan_operation_id": item["plan_operation_id"],
                }
                for item in await goal_draft_items(db, conversation, limit=10)
            ]
        return {
            "operation": {**public_operation(op), "payload": op.payload, "versions": op.versions},
            "user": public_user(user),
            "goal": serialize(goal) if goal else None,
            "goal_drafts": draft_states,
            "goals": [
                {"id": item.id, "title": item.title, "outcome": item.outcome[:500], "status": item.status}
                for item in await db.scalars(
                    select(Goal).where(Goal.user_id == user.id).order_by(Goal.created_at.desc()).limit(30)
                )
            ]
            if goal is None
            else [],
            "plan": plan_data,
            "tasks": task_data,
            "submission": serialize(submission) if submission else None,
            "attachments": attachments,
            "budgets": budgets,
            "skill_progress": [serialize(item) for item in progress],
            "evidence": [
                {
                    "id": item.id,
                    "submission_id": item.submission_id,
                    "passed": item.passed,
                    "feedback": item.feedback[:2000],
                    "evidence_type": item.evidence_type,
                    "created_at": item.created_at.isoformat(),
                }
                for item in evidence
            ],
            "messages": list(reversed(message_data)),
            "memories": [
                {"id": m.id, "version": m.version, "suppressed": m.suppressed, "deleted": m.deleted}
                for m in memories
            ],
            "memory_bank_id": f"learning-{user.id}",
            "memory_recall_enabled": user.memory_recall_enabled,
            "agent_history": op.agent_history
            or (
                await db.scalar(
                    select(Operation.agent_history)
                    .where(
                        Operation.goal_id == op.goal_id,
                        Operation.user_id == user.id,
                        Operation.kind == "tutor",
                        Operation.status == "succeeded",
                    )
                    .order_by(Operation.created_at.desc())
                    .limit(1)
                )
            )
            or [],
            "today": now().astimezone(ZoneInfo(user.timezone)).date().isoformat(),
        }


async def operation_locked(db, operation_id):
    op = await db.get(Operation, operation_id)
    if op is None:
        raise DomainError("not_found", "操作不存在", 404)
    user, stream = await lock_user(db, op.user_id)
    # Refresh after the per-user lock; another transaction may have completed while we waited.
    await db.refresh(op)
    goal = await db.get(Goal, op.goal_id) if op.goal_id else None
    return op, user, stream, goal


async def mark_running(operation_id: str, stage: str = "processing") -> dict:
    async with SessionFactory() as db, db.begin():
        op, user, stream, goal = await operation_locked(db, operation_id)
        if op.status in TERMINAL:
            return public_operation(op)
        op.status, op.stage, op.updated_at = "running", stage[:100], now()
        await emit(db, stream, "operation.started", op.id, "Agent 正在处理", op.goal_id, op.id)
        return public_operation(op)


async def mark_failed(operation_id: str, code: str, message: str) -> dict:
    async with SessionFactory() as db, db.begin():
        op, user, stream, goal = await operation_locked(db, operation_id)
        if op.status not in TERMINAL:
            op.status, op.stage, op.updated_at = "failed", "failed", now()
            # Never surface provider exception strings or arbitrary secrets.
            messages = {
                "model_timeout": "模型响应超时，未能完成本次处理。已保存的内容仍然保留，可以重试。",
                "operation_timeout": "本次处理已达到 15 分钟时限。已保存的内容仍然保留，可以重试。",
            }
            op.error_code = code[:60]
            op.error_message = messages.get(code, "处理未完成，已保存原始内容，可以重试。")
            await emit(db, stream, "operation.failed", op.id, op.error_message, op.goal_id, op.id)
        return public_operation(op)


async def contact_allowed(db, user, goal):
    settings = get_settings()
    local = now().astimezone(ZoneInfo(user.timezone))
    if not user.proactive_enabled or goal.status != "active":
        return False, "disabled"
    start, end, hour = user.quiet_start, user.quiet_end, local.hour
    quiet = (start <= hour < end) if start < end else (hour >= start or hour < end) if start > end else False
    if quiet:
        return False, "quiet_hours"
    today_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    count = await db.scalar(
        select(func.count())
        .select_from(Message)
        .join(Conversation)
        .where(
            Conversation.user_id == user.id, Message.proactive.is_(True), Message.created_at >= today_start
        )
    )
    if count >= settings.proactive_daily_limit:
        return False, "daily_limit"
    latest = await db.scalar(
        select(func.max(Message.created_at))
        .join(Conversation)
        .where(Conversation.goal_id == goal.id, Message.proactive.is_(True))
    )
    if latest is not None:
        if latest.tzinfo is None:
            from datetime import timezone

            latest = latest.replace(tzinfo=timezone.utc)
        if now() - latest < timedelta(hours=settings.proactive_gap_hours):
            return False, "minimum_gap"
    return True, None


async def valid_general_tutor(db, op, user):
    if op.goal_id is not None or op.kind != "tutor" or not op.payload.get("conversation_id"):
        return False
    conversation = await db.get(Conversation, op.payload["conversation_id"])
    return bool(conversation and conversation.user_id == user.id and conversation.goal_id is None)


async def commit_result(operation_id: str, result: dict) -> dict:
    async with SessionFactory() as db, db.begin():
        op, user, stream, goal = await operation_locked(db, operation_id)
        if op.status in TERMINAL:
            return public_operation(op)
        reason = None
        versions = op.versions
        global_tutor = await valid_general_tutor(db, op, user)
        if (goal is None and not global_tutor) or (goal and goal.status in {"cancelled", "completed"}):
            reason = "goal_terminal"
        elif op.kind != "tutor" and (
            goal.status == "paused" or versions.get("control_revision") != goal.control_revision
        ):
            reason = "goal_control_changed"
        elif op.kind in {"plan", "recommendation"} and (
            versions.get("availability_version") != user.availability_version
            or versions.get("active_plan_id") != goal.active_plan_id
        ):
            reason = "planning_context_changed"
        elif versions.get("memory_control_revision") != user.memory_control_revision:
            reason = "memory_changed"
        if op.proactive and reason is None:
            allowed, reason = await contact_allowed(db, user, goal)
            if allowed:
                reason = None
        if reason:
            op.status, op.stage, op.result = "superseded", "superseded", {"reason": reason}
            await emit(
                db, stream, "operation.superseded", op.id, "上下文已变化，旧结果未发布", op.goal_id, op.id
            )
            return public_operation(op)
        if result.get("kind", op.kind) != op.kind:
            raise DomainError("invalid_result", "Agent 输出类型与操作不符", 422)
        draft = result.get("goal_draft")
        if draft is not None:
            if not global_tutor or result.get("follow_up") is not None:
                raise DomainError("invalid_goal_draft", "仅个人对话可以提出新目标草稿", 422)
            try:
                draft = GoalDraft.model_validate(draft).model_dump(mode="json")
            except ValidationError:
                raise DomainError("invalid_goal_draft", "目标草稿字段不完整或无效", 422) from None
        content = str(result.get("message") or result.get("summary") or "处理完成。")[:32000]
        resource_id = None
        task_id = None
        if op.kind == "plan":
            proposal = PlanCandidate.model_validate(result)
            current_version = (
                await db.scalar(select(func.max(Plan.version)).where(Plan.goal_id == goal.id)) or 0
            )
            proposal_data = proposal.model_dump(mode="json")
            if op.payload.get("today_minutes") is not None:
                proposal_data["remaining_today_minutes"] = op.payload["today_minutes"]
                proposal_data["cap_date"] = op.payload["cap_date"]
                await check_plan_cap(db, user, goal, proposal_data)
            plan = Plan(
                goal_id=goal.id,
                version=current_version + 1,
                summary=proposal.summary,
                proposal=proposal_data,
                availability_version=user.availability_version,
                base_plan_id=goal.active_plan_id,
            )
            db.add(plan)
            await db.flush()
            resource_id, content = plan.id, proposal.message
            if proposal_data.get("remaining_today_minutes") is not None:
                content += f"\n{proposal_data['cap_date']} 剩余可安排上限：{proposal_data['remaining_today_minutes']} 分钟；已投入时间仍保留。"
                plan.proposal = {**proposal_data, "message": content}
            await emit(
                db, stream, "plan.proposed", plan.id, "学习计划已生成，等待确认", goal.id, op.id, plan.version
            )
        elif op.kind == "assessment":
            submission = await db.get(Submission, op.payload["submission_id"])
            if result.get("submission_id", submission.id) != submission.id:
                raise DomainError("invalid_result", "评估引用的提交不匹配", 422)
            task = await db.get(Task, submission.task_id)
            valid = (
                task.active_submission_id == submission.id
                and task.requirements_version == submission.requirements_version
            )
            if not isinstance(result.get("passed"), bool) or not isinstance(result.get("feedback"), str):
                raise DomainError("invalid_result", "评估缺少结果或反馈", 422)
            evaluation = Evaluation(
                submission_id=submission.id,
                operation_id=op.id,
                passed=result["passed"],
                feedback=result["feedback"][:32000],
                evidence_type="ai_review",
                rubric_version=submission.requirements_version,
                valid=valid,
                details={"skills": result.get("skills", []), "limitations": "AI 审阅，未执行用户代码"},
            )
            db.add(evaluation)
            await db.flush()
            resource_id, task_id, content = evaluation.id, task.id, evaluation.feedback
            if valid:
                task.status = "completed" if evaluation.passed else "needs_revision"
                task.state_version += 1
                if evaluation.passed:
                    await reserve(db, user, task.scheduled_date, -task.reserved_minutes)
                    task.reserved_minutes = 0
                for item in result.get("skills", [])[:20]:
                    if not isinstance(item, dict) or item.get("name") not in task.skills:
                        continue
                    progress = await db.scalar(
                        select(SkillProgress).where(
                            SkillProgress.goal_id == goal.id, SkillProgress.name == item["name"]
                        )
                    )
                    if progress is None:
                        progress = SkillProgress(user_id=user.id, goal_id=goal.id, name=item["name"])
                        db.add(progress)
                    progress.status = (
                        "evidence_available"
                        if item.get("passed") is True and evaluation.passed
                        else "needs_review"
                    )
                    progress.evaluation_id = evaluation.id
                    progress.review_at = now() + timedelta(
                        days=3 if progress.status == "evidence_available" else 1
                    )
                if task.milestone_id:
                    milestone = await db.get(Milestone, task.milestone_id)
                    siblings = list(
                        (await db.scalars(select(Task).where(Task.milestone_id == milestone.id))).all()
                    )
                    milestone.status = (
                        "evidence_available"
                        if siblings and all(t.status == "completed" for t in siblings)
                        else "practicing"
                    )
                from app.domain.commands import memory_outbox

                source = MemorySource(
                    user_id=user.id,
                    source_id=evaluation.id,
                    source_type="evaluation",
                    content=evaluation.feedback,
                )
                db.add(source)
                await db.flush()
                await memory_outbox(db, source, "retain")
            else:
                # Retain old evaluation as history, never overwrite the latest submission or publish old feedback.
                op.status, op.stage, op.result = "superseded", "superseded", {"evaluation_id": evaluation.id}
                await emit(
                    db,
                    stream,
                    "evaluation.archived",
                    evaluation.id,
                    "早期提交的评估已保存到历史",
                    goal.id,
                    op.id,
                )
                return public_operation(op)
            await emit(
                db,
                stream,
                "evaluation.completed",
                evaluation.id,
                "评估已保存，请查看反馈与证据",
                goal.id,
                op.id,
            )
        elif op.kind == "recommendation":
            if result.get("task_id"):
                referenced = await db.get(Task, result["task_id"])
                if (
                    not referenced
                    or referenced.goal_id != goal.id
                    or referenced.status not in {"ready", "in_progress", "needs_revision"}
                ):
                    raise DomainError("invalid_task", "推荐引用的任务已失效", 422)
                task_id = referenced.id
            if result.get("task"):
                plan = await db.get(Plan, goal.active_plan_id)
                milestones = list(
                    (
                        await db.scalars(
                            select(Milestone).where(Milestone.plan_id == plan.id).order_by(Milestone.position)
                        )
                    ).all()
                )
                candidate = TaskCandidate.model_validate(result["task"])
                if candidate.milestone_index >= len(milestones):
                    raise DomainError("invalid_result", "推荐任务的里程碑无效", 422)
                task = await insert_task(
                    db, user, goal, plan, result["task"], milestones[candidate.milestone_index].id
                )
                task_id = resource_id = task.id
                await emit(db, stream, "task.recommended", task.id, "已准备下一步学习任务", goal.id, op.id)
        elif op.kind != "tutor":
            raise DomainError("invalid_operation", "不支持的操作类型", 422)
        msg = await message(
            db,
            stream,
            goal,
            op,
            content,
            kind="proactive"
            if op.proactive
            else "proposal"
            if op.kind == "plan"
            else "result"
            if op.kind == "assessment"
            else "assistant_reply",
            task_id=task_id,
        )
        op.status, op.stage, op.updated_at = "succeeded", "saved", now()
        op.result = {"resource_id": resource_id, "message_id": msg.id}
        if draft is not None:
            previous = await db.scalars(
                select(Operation).where(
                    Operation.user_id == user.id,
                    Operation.goal_id.is_(None),
                    Operation.kind == "tutor",
                    Operation.status == "succeeded",
                    Operation.payload["conversation_id"].as_string() == op.payload["conversation_id"],
                    Operation.result["goal_draft_status"].as_string() == "pending",
                )
            )
            for prior in previous:
                prior.result = {**prior.result, "goal_draft_status": "superseded"}
                prior.updated_at = now()
                await emit(
                    db, stream, "goal_draft.superseded", prior.id, "目标草稿已有更新", operation_id=prior.id
                )
            op.result = {**op.result, "goal_draft": draft, "goal_draft_status": "pending"}
            await emit(
                db, stream, "goal_draft.proposed", op.id, "目标草稿已准备，请检查并确认", operation_id=op.id
            )
        follow_up = result.get("follow_up") if op.kind == "tutor" else None
        if isinstance(follow_up, dict) and goal is not None and goal.status in {"draft", "active"}:
            kind = follow_up.get("kind")
            if kind not in {"plan", "recommendation"}:
                raise DomainError("invalid_follow_up", "后续操作类型无效", 422)
            payload = {
                "instruction": str(follow_up.get("instruction", ""))[:8000],
                "source_operation_id": op.id,
            }
            cap = follow_up.get("today_minutes")
            if cap is not None:
                if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0 or cap > 720:
                    raise DomainError("invalid_follow_up", "剩余学习时间无效", 422)
                kind = "plan"
                row = await budget(db, user)
                payload.update(
                    today_minutes=min(cap, max(0, row.authorized_minutes - row.consumed_minutes)),
                    cap_date=row.local_date.isoformat(),
                )
            if kind == "plan" or goal.status == "active":
                follow, created = await new_operation(
                    db, user, stream, f"tutor-followup:{op.id}", kind, payload, goal
                )
                if created:
                    await dispatch(
                        db, follow, "goal_created" if kind == "plan" else "recommendation_requested"
                    )
                op.result = {**op.result, "follow_up_operation_id": follow.id}
        if op.kind == "assessment":
            allowed, _ = await contact_allowed(db, user, goal)
            if allowed:
                follow, created = await new_operation(
                    db,
                    user,
                    stream,
                    f"assessment-followup:{op.id}",
                    "recommendation",
                    {
                        "trigger": "assessment_completed",
                        "submission_id": submission.id,
                        "review_source_task_id": task.id,
                        "instruction": "根据刚完成的有效评估推荐下一步，薄弱项可安排有来源的复习；预算已满时仅提醒已有任务。",
                    },
                    goal,
                    proactive=True,
                )
                if created:
                    await dispatch(db, follow, "recommendation_requested")
                op.result = {**op.result, "follow_up_operation_id": follow.id}
        history = result.get("agent_history", [])
        if isinstance(history, list):
            import json

            if len(json.dumps(history)) <= 256000:
                op.agent_history = history
        await emit(
            db, stream, "operation.succeeded", op.id, "处理结果已保存", goal.id if goal else None, op.id
        )
        return public_operation(op)


async def record_model_usage(operation_id: str, usage: dict) -> dict:
    async with SessionFactory() as db, db.begin():
        op, user, stream, goal = await operation_locked(db, operation_id)
        for column, key in (
            ("model_requests", "requests"),
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
        ):
            setattr(op, column, max(getattr(op, column), max(0, int(usage.get(key, 0)))))
        return {
            "requests": op.model_requests,
            "input_tokens": op.input_tokens,
            "output_tokens": op.output_tokens,
        }


async def claim_outbox(owner: str, limit: int = 20) -> list[dict]:
    async with SessionFactory() as db, db.begin():
        rows = list(
            (
                await db.scalars(
                    select(Outbox)
                    .where(
                        Outbox.status != "delivered",
                        Outbox.next_attempt_at <= now(),
                        or_(Outbox.lease_until.is_(None), Outbox.lease_until <= now()),
                    )
                    .order_by(Outbox.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for row in rows:
            row.lease_owner, row.lease_until, row.status = owner, now() + timedelta(seconds=90), "leased"
            row.attempts += 1
        await db.flush()
        return [serialize(row) for row in rows]


async def ack_outbox(outbox_id: str, owner: str):
    async with SessionFactory() as db, db.begin():
        row = await db.get(Outbox, outbox_id, with_for_update=True)
        if row and row.lease_owner == owner:
            row.status, row.lease_until, row.lease_owner = "delivered", None, None


async def retry_outbox(outbox_id: str, owner: str, error: str):
    async with SessionFactory() as db, db.begin():
        row = await db.get(Outbox, outbox_id, with_for_update=True)
        if row and row.lease_owner == owner:
            row.status, row.lease_until, row.lease_owner = "pending", None, None
            if error == "ConversationOrderPending":
                row.next_attempt_at = now() + timedelta(seconds=1)
                row.attempts = max(0, row.attempts - 1)
                row.last_error = None
            else:
                row.next_attempt_at = now() + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
                row.last_error = "Delivery failed; consult service diagnostics with outbox id."


async def prepare_proactive(user_id: str, goal_id: str) -> dict:
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        goal = await db.get(Goal, goal_id)
        if goal is None or goal.user_id != user_id:
            return {"status": "closed", "reason": "not_found", "next_check_seconds": 3600}
        if goal.status in {"cancelled", "completed"}:
            return {"status": "closed", "reason": goal.status}
        allowed, reason = await contact_allowed(db, user, goal)
        local = now().astimezone(ZoneInfo(user.timezone))
        if not allowed or local.hour < user.learning_hour:
            return {
                "status": "skipped",
                "reason": reason or "before_learning_hour",
                "next_check_seconds": 3600,
            }
        created_at = goal.created_at
        if created_at.tzinfo is None:
            from datetime import timezone

            created_at = created_at.replace(tzinfo=timezone.utc)
        if now() - created_at >= timedelta(days=7):
            iso = local.isocalendar()
            review_key = f"weekly:{goal.id}:{iso.year}:{iso.week}"
            existing_review = await db.scalar(
                select(Operation.id).where(
                    Operation.user_id == user.id, Operation.idempotency_key == review_key
                )
            )
            if existing_review is None:
                review, _ = await new_operation(
                    db,
                    user,
                    stream,
                    review_key,
                    "plan",
                    {
                        "trigger": "weekly_review",
                        "instruction": "基于有效学习证据总结本周掌握、困难与投入，提出未来7天调整；保留目标边界，等待用户确认。",
                    },
                    goal,
                    proactive=True,
                )
                return {"operation_id": review.id, "status": review.status, "next_check_seconds": 3600}
        row = await budget(db, user)
        pending_task = await db.scalar(
            select(Task.id)
            .where(
                Task.goal_id == goal.id,
                Task.scheduled_date == local.date(),
                Task.status.in_(["ready", "in_progress", "needs_revision"]),
            )
            .limit(1)
        )
        if row.consumed_minutes + row.reserved_minutes >= row.authorized_minutes and pending_task is None:
            return {"status": "skipped", "reason": "budget_full", "next_check_seconds": 3600}
        key = f"timer:{goal.id}:{local.date()}"
        op, created = await new_operation(
            db, user, stream, key, "recommendation", {"trigger": "timer_due"}, goal, True
        )
        if not created:
            return {"status": "skipped", "reason": "already_prepared", "next_check_seconds": 3600}
        # Already in GoalWorkflow, so caller directly handles the returned operation.
        return {"operation_id": op.id, "status": op.status, "next_check_seconds": 3600}


async def load_memory_source(source_id: str) -> dict:
    async with SessionFactory() as db:
        source = await db.get(MemorySource, source_id)
        if source is None:
            raise DomainError("not_found", "记忆来源不存在", 404)
        return {**serialize(source), "bank_id": f"learning-{source.user_id}"}


async def ack_memory_sync(source_id: str, version: int, external_id: str | None = None) -> dict:
    async with SessionFactory() as db, db.begin():
        source = await db.get(MemorySource, source_id)
        user, stream = await lock_user(db, source.user_id)
        await db.refresh(source)
        if source.version == version:
            source.status = "suppressed" if source.suppressed else "succeeded"
            source.external_id = external_id
            await db.flush()
            remaining = await db.scalar(
                select(func.count())
                .select_from(MemorySource)
                .where(
                    MemorySource.user_id == user.id,
                    MemorySource.suppressed.is_(True),
                    MemorySource.status != "suppressed",
                )
            )
            if remaining == 0:
                user.memory_recall_enabled = True
            await emit(db, stream, "memory.sync_changed", source.id, "记忆同步状态已更新")
        return serialize(source)


async def mark_memory_failed(source_id: str, version: int) -> dict:
    async with SessionFactory() as db, db.begin():
        source = await db.get(MemorySource, source_id)
        user, stream = await lock_user(db, source.user_id)
        await db.refresh(source)
        if source.version == version:
            source.status = "failed"
            await emit(db, stream, "memory.sync_changed", source.id, "记忆同步暂未完成，可手动重试")
        return serialize(source)


async def reserve_model_request(operation_id: str, limit: int = 6) -> dict:
    async with SessionFactory() as db, db.begin():
        op, user, stream, goal = await operation_locked(db, operation_id)
        created = op.created_at
        if created.tzinfo is None:
            from datetime import timezone

            created = created.replace(tzinfo=timezone.utc)
        if op.status in TERMINAL:
            raise DomainError("operation_terminal", "操作已结束")
        if now() - created > timedelta(minutes=15):
            raise DomainError("operation_deadline", "操作超过最大执行时间")
        if op.model_requests >= min(limit, 6):
            raise DomainError("model_budget_exceeded", "该操作的模型调用预算已用完")
        global_tutor = await valid_general_tutor(db, op, user)
        if (not goal and not global_tutor) or (goal and goal.status in {"cancelled", "completed"}):
            raise DomainError("goal_terminal", "目标已结束")
        if op.kind != "tutor" and (
            goal.status == "paused" or goal.control_revision != op.versions.get("control_revision")
        ):
            raise DomainError("goal_control_changed", "目标控制状态已变化")
        if user.memory_control_revision != op.versions.get("memory_control_revision"):
            raise DomainError("memory_changed", "记忆已更新，旧请求不能继续")
        if op.kind in {"plan", "recommendation"} and user.availability_version != op.versions.get(
            "availability_version"
        ):
            raise DomainError("availability_changed", "时间预算已变化")
        from app.integrations.model_catalog import request_model

        model_alias = await request_model(db, op.versions.get("model_alias"))
        op.model_requests += 1
        return {"requests": op.model_requests, "limit": min(limit, 6), "model_alias": model_alias}


async def record_model_tokens(operation_id: str, input_tokens: int, output_tokens: int) -> dict:
    async with SessionFactory() as db, db.begin():
        op, user, stream, goal = await operation_locked(db, operation_id)
        op.input_tokens += max(0, input_tokens)
        op.output_tokens += max(0, output_tokens)
        return {"input_tokens": op.input_tokens, "output_tokens": op.output_tokens}


async def get_dispatch_state(payload: dict) -> dict:
    async with SessionFactory() as db:
        goal = await db.get(Goal, payload.get("goal_id")) if payload.get("goal_id") else None
        op = await db.get(Operation, payload.get("operation_id")) if payload.get("operation_id") else None
        if goal is None and op is not None and op.user_id == payload.get("user_id"):
            account = await db.get(User, op.user_id)
            if await valid_general_tutor(db, op, account):
                first_pending = None
                if op.status not in TERMINAL:
                    first_pending = await db.scalar(
                        select(Operation.id)
                        .where(
                            Operation.user_id == op.user_id,
                            Operation.goal_id.is_(None),
                            Operation.kind == "tutor",
                            Operation.status.not_in(TERMINAL),
                        )
                        .order_by(Operation.created_at, Operation.id)
                        .limit(1)
                    )
                return {
                    "goal_status": "general",
                    "operation_status": op.status,
                    "blocked_by_prior_operation": first_pending is not None and first_pending != op.id,
                }
        valid = goal is not None and goal.user_id == payload.get("user_id")
        return {
            "goal_status": goal.status if valid else "missing",
            "operation_status": op.status if valid and op and op.user_id == goal.user_id else "missing",
        }


async def get_memory_recall_scope(operation_id: str, source_ids: list[str] | None = None) -> dict:
    """Small read boundary for recall; do not reload large prompts or parse attachments."""
    async with SessionFactory() as db:
        op = await db.get(Operation, operation_id)
        if op is None:
            raise DomainError("not_found", "操作不存在", 404)
        user = await db.get(User, op.user_id)
        sources = []
        if source_ids:
            rows = await db.scalars(
                select(MemorySource).where(
                    MemorySource.user_id == user.id,
                    MemorySource.id.in_(source_ids[:100]),
                    MemorySource.suppressed.is_(False),
                    MemorySource.deleted.is_(False),
                )
            )
            sources = [
                {"id": row.id, "version": row.version, "suppressed": False, "deleted": False} for row in rows
            ]
        goal = await db.get(Goal, op.goal_id) if op.goal_id else None
        allowed = (
            (
                (
                    goal is not None
                    and goal.user_id == user.id
                    and goal.status not in {"cancelled", "completed"}
                )
                or await valid_general_tutor(db, op, user)
            )
            and op.status not in TERMINAL
            and user.memory_control_revision == op.versions.get("memory_control_revision")
            and (
                op.kind == "tutor"
                or (goal.status != "paused" and goal.control_revision == op.versions.get("control_revision"))
            )
        )
        return {
            "bank_id": f"learning-{user.id}",
            "memory_control_revision": user.memory_control_revision,
            "enabled": user.memory_recall_enabled and allowed,
            "sources": sources if allowed else [],
        }
