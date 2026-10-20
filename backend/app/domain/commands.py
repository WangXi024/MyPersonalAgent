from datetime import date
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select

from app.agents.schemas import GoalDraft
from app.db.models import (
    Attachment,
    Conversation,
    Evaluation,
    Goal,
    MemorySource,
    Message,
    Milestone,
    Operation,
    Outbox,
    Plan,
    StudyRecord,
    Submission,
    Task,
    now,
)
from app.db.session import SessionFactory
from app.domain.common import (
    DomainError,
    budget,
    digest,
    dispatch,
    emit,
    lock_user,
    message,
    new_operation,
    owned,
    reserve,
    serialize,
    version,
)


def accepted(op, resource_id=None):
    return {"operation_id": op.id, "status": op.status, "resource_id": resource_id or op.goal_id}


def require_active(goal):
    if goal.status != "active":
        raise DomainError("goal_inactive", "请先确认计划或恢复目标")


async def create_goal(user_id, key, payload):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        return await create_goal_in_transaction(db, user, stream, key, payload)


async def create_goal_in_transaction(db, user, stream, key, payload):
    existing = await db.scalar(
        select(Operation).where(Operation.user_id == user.id, Operation.idempotency_key == key)
    )
    if existing:
        if existing.request_hash != digest({"kind": "plan", "goal_id": None, "payload": payload}):
            raise DomainError("idempotency_conflict", "幂等键已用于不同请求")
        return accepted(existing)
    op, _ = await new_operation(db, user, stream, key, "plan", payload)
    goal = Goal(user_id=user.id, **payload)
    if isinstance(goal.deadline, str):
        goal.deadline = date.fromisoformat(goal.deadline)
    db.add(goal)
    await db.flush()
    db.add(Conversation(user_id=user.id, goal_id=goal.id))
    op.goal_id = goal.id
    op.versions = {**op.versions, "control_revision": goal.control_revision}
    await dispatch(db, op, "goal_created")
    await emit(db, stream, "goal.created", goal.id, "学习目标已创建", goal.id, op.id)
    return accepted(op)


async def goal_draft_items(db, conversation, limit=30):
    if conversation.goal_id is not None:
        return []
    operations = await db.scalars(
        select(Operation)
        .join(Message, Message.operation_id == Operation.id)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
            Operation.user_id == conversation.user_id,
            Operation.goal_id.is_(None),
            Operation.kind == "tutor",
            Operation.status == "succeeded",
            Operation.result["goal_draft_status"].as_string().in_(["pending", "accepted", "superseded"]),
        )
        .order_by(Message.seq.desc())
        .limit(limit)
    )
    return list(
        reversed(
            [
                {
                    "operation_id": op.id,
                    "message_id": op.result["message_id"],
                    "status": op.result["goal_draft_status"],
                    "draft": op.result["goal_draft"],
                    "goal_id": op.result.get("goal_id"),
                    "plan_operation_id": op.result.get("plan_operation_id"),
                }
                for op in operations
            ]
        )
    )


async def accept_goal_draft(user_id, operation_id, key, payload):
    try:
        payload = GoalDraft.model_validate(payload).model_dump(mode="json")
    except ValidationError:
        raise DomainError("invalid_goal_draft", "请补全目标背景、成果和验收标准", 422) from None
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        source = await owned(db, Operation, operation_id, user_id)
        conversation_id = source.payload.get("conversation_id")
        conversation = await db.get(Conversation, conversation_id) if conversation_id else None
        if (
            source.goal_id is not None
            or source.kind != "tutor"
            or source.status != "succeeded"
            or conversation is None
            or conversation.user_id != user_id
            or conversation.goal_id is not None
            or not isinstance(source.result.get("goal_draft"), dict)
        ):
            raise DomainError("goal_draft_unavailable", "该操作没有可确认的个人目标草稿")
        payload_hash = digest(payload)
        draft_status = source.result.get("goal_draft_status")
        if draft_status == "accepted":
            if source.result.get("goal_draft_accept_hash") != payload_hash:
                raise DomainError("goal_draft_conflict", "草稿已经按其他内容确认，请打开已创建目标修改")
        elif draft_status != "pending":
            raise DomainError("goal_draft_superseded", "该草稿已被更新，请确认最新草稿")
        confirmation, created = await new_operation(
            db,
            user,
            stream,
            key,
            "control",
            {"action": "accept_goal_draft", "source_operation_id": source.id, "draft": payload},
        )
        if not created:
            return confirmation.result["acceptance"]
        if draft_status == "accepted":
            response = source.result["goal_draft_acceptance"]
        else:
            plan_key = f"goal-draft:{source.id}"
            if await db.scalar(
                select(Operation.id).where(
                    Operation.user_id == user_id, Operation.idempotency_key == plan_key
                )
            ):
                raise DomainError("idempotency_conflict", "规划操作键已被占用")
            response = await create_goal_in_transaction(db, user, stream, plan_key, payload)
            planning = await db.get(Operation, response["operation_id"])
            planning.payload = {**planning.payload, "source_operation_id": source.id}
            source.result = {
                **source.result,
                "goal_draft": payload,
                "goal_draft_status": "accepted",
                "goal_id": response["resource_id"],
                "plan_operation_id": response["operation_id"],
                "goal_draft_accept_hash": payload_hash,
                "goal_draft_acceptance": response,
            }
            source.updated_at = now()
            await emit(
                db,
                stream,
                "goal_draft.accepted",
                source.id,
                "目标草稿已确认，开始准备学习计划",
                response["resource_id"],
                source.id,
            )
        confirmation.status, confirmation.stage = "succeeded", "saved"
        confirmation.result = {"acceptance": response}
        return response


async def request_operation(user_id, key, goal_id, kind, payload, proactive=False):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        goal = await owned(db, Goal, goal_id, user_id)
        if goal.status in {"cancelled", "completed"}:
            raise DomainError("goal_terminal", "该目标已结束")
        if kind != "tutor" and goal.status == "paused":
            raise DomainError("goal_paused", "请先恢复目标")
        if kind == "recommendation":
            require_active(goal)
        op, created = await new_operation(db, user, stream, key, kind, payload, goal, proactive)
        if created:
            await dispatch(
                db,
                op,
                {
                    "plan": "goal_created",
                    "tutor": "user_message",
                    "recommendation": "recommendation_requested",
                }[kind],
            )
        return accepted(op)


async def control_goal(user_id, goal_id, action, expected_version, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        goal = await owned(db, Goal, goal_id, user_id)
        return await control_goal_in_transaction(db, user, stream, goal, action, expected_version, key)


async def control_goal_in_transaction(db, user, stream, goal, action, expected_version, key, source_operation_id=None):
    op, created = await new_operation(
        db, user, stream, key, "control", {"action": action, "expected_version": expected_version}, goal
    )
    if not created:
        return serialize(goal)
    version(goal.control_revision, expected_version)
    allowed = {
        "pause": ({"active"}, "paused"),
        "resume": ({"paused"}, "active"),
        "cancel": ({"draft", "active", "paused"}, "cancelled"),
        "complete": ({"active"}, "completed"),
    }
    source, target = allowed[action]
    if goal.status not in source:
        raise DomainError("invalid_transition", "当前目标状态不允许此操作")
    if action == "complete":
        milestones = list(
            (
                await db.scalars(
                    select(Milestone).where(
                        Milestone.goal_id == goal.id, Milestone.plan_id == goal.active_plan_id
                    )
                )
            ).all()
        )
        if not milestones or any(m.status != "evidence_available" for m in milestones):
            raise DomainError("insufficient_evidence", "仍有里程碑缺少完成证据")
    goal.status, goal.control_revision = target, goal.control_revision + 1
    op.status, op.stage = "succeeded", "saved"
    if action in {"pause", "cancel", "complete"}:
        pending = await db.scalars(
            select(Operation).where(
                Operation.goal_id == goal.id,
                Operation.id != source_operation_id,
                Operation.status.in_(["accepted", "running", "waiting_user"]),
            )
        )
        for active in pending:
            if action == "pause" and active.kind == "tutor":
                continue
            active.status = "superseded" if action == "pause" else "cancelled"
            active.stage, active.updated_at = active.status, now()
            active.result = {"reason": f"goal_{target}"}
            await emit(
                db,
                stream,
                f"operation.{active.status}",
                active.id,
                "目标状态已变化，该操作已停止",
                goal.id,
                active.id,
            )
    if action == "resume":
        awaiting = await db.scalars(
            select(Task).where(
                Task.goal_id == goal.id,
                Task.status == "awaiting_review",
                Task.active_submission_id.is_not(None),
            )
        )
        for task in awaiting:
            submission = await db.get(Submission, task.active_submission_id)
            if submission is None or submission.requirements_version != task.requirements_version:
                continue
            evaluated = await db.scalar(
                select(Evaluation.id)
                .where(Evaluation.submission_id == submission.id, Evaluation.valid.is_(True))
                .limit(1)
            )
            if evaluated is not None:
                continue
            previous = await db.scalar(
                select(Operation)
                .where(
                    Operation.goal_id == goal.id,
                    Operation.kind == "assessment",
                    Operation.status == "superseded",
                    Operation.payload["submission_id"].as_string() == submission.id,
                    Operation.result["reason"].as_string() == "goal_paused",
                )
                .order_by(Operation.created_at.desc())
                .limit(1)
            )
            if previous is None:
                continue
            resumed, created = await new_operation(
                db,
                user,
                stream,
                f"resume-assessment:{goal.id}:{goal.control_revision}:{submission.id}",
                "assessment",
                {**previous.payload, "resumed_from_operation_id": previous.id},
                goal,
            )
            if created:
                resumed.versions = {**resumed.versions, "requirements_version": task.requirements_version}
                await dispatch(db, resumed, "submission_created")
    if action in {"cancel", "complete"}:
        tasks = await db.scalars(
            select(Task).where(
                Task.goal_id == goal.id, Task.status.notin_(["completed", "cancelled", "skipped"])
            )
        )
        for task in tasks:
            await reserve(db, user, task.scheduled_date, -task.reserved_minutes)
            task.reserved_minutes = 0
            task.status = "cancelled"
            task.state_version += 1
    await emit(
        db,
        stream,
        f"goal.{target}",
        goal.id,
        {"pause": "目标已暂停", "resume": "目标已恢复", "cancel": "目标已取消", "complete": "目标已完成"}[
            action
        ],
        goal.id,
        op.id,
        goal.control_revision,
    )
    await dispatch(
        db,
        op,
        {
            "pause": "goal_paused",
            "resume": "goal_resumed",
            "cancel": "goal_cancelled",
            "complete": "goal_completed",
        }[action],
    )
    return serialize(goal)


async def accept_plan(user_id, plan_id, expected_version, accept, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        plan = await owned(db, Plan, plan_id, user_id)
        goal = await owned(db, Goal, plan.goal_id, user_id)
        op, created = await new_operation(
            db,
            user,
            stream,
            key,
            "plan_accept" if accept else "plan_reject",
            {"plan_id": plan_id, "expected_version": expected_version},
            goal,
        )
        if not created:
            return serialize(plan)
        version(plan.version, expected_version)
        if plan.status != "proposed" or goal.status in {"cancelled", "completed", "paused"}:
            raise DomainError("plan_unavailable", "该计划当前不能确认")
        if not accept:
            plan.status = "rejected"
        else:
            version(user.availability_version, plan.availability_version)
            if plan.base_plan_id != goal.active_plan_id:
                raise DomainError("version_conflict", "目标已有更新的计划")
            from app.domain.execution import check_plan_cap

            await check_plan_cap(db, user, goal, plan.proposal)
            old_tasks = list(
                (
                    await db.scalars(
                        select(Task).where(
                            Task.goal_id == goal.id, Task.status.in_(["ready", "needs_revision"])
                        )
                    )
                ).all()
            )
            previous_states = {task.id: task.status for task in old_tasks}
            for task in old_tasks:
                await reserve(db, user, task.scheduled_date, -task.reserved_minutes)
                task.reserved_minutes = 0
                task.status = "cancelled"
                task.state_version += 1
            if goal.active_plan_id:
                old = await db.get(Plan, goal.active_plan_id)
                old.status = "superseded"
                await db.flush()
            milestones = []
            for index, item in enumerate(plan.proposal["milestones"]):
                milestone = Milestone(
                    goal_id=goal.id,
                    plan_id=plan.id,
                    position=index,
                    title=item["title"],
                    criteria=item["criteria"],
                )
                db.add(milestone)
                await db.flush()
                milestones.append(milestone)
            from app.domain.execution import TaskCandidate, insert_task

            for item in plan.proposal["tasks"]:
                index = item.get("milestone_index", 0)
                if index < 0 or index >= len(milestones):
                    raise DomainError("invalid_plan", "任务关联的里程碑无效", 422)
                candidate = TaskCandidate.model_validate(item)
                reusable = next(
                    (
                        task
                        for task in old_tasks
                        if task.title == candidate.title
                        and task.instructions == candidate.instructions
                        and task.criteria == candidate.criteria
                        and task.skills == candidate.skills
                        and task.review_of_task_id == candidate.review_of_task_id
                    ),
                    None,
                )
                if reusable is None:
                    await insert_task(db, user, goal, plan, item, milestones[index].id)
                else:
                    if candidate.scheduled_date < now().astimezone(ZoneInfo(user.timezone)).date():
                        raise DomainError("stale_schedule", "任务日期已过期，需要重新规划")
                    await reserve(db, user, candidate.scheduled_date, candidate.estimated_minutes)
                    reusable.plan_id, reusable.milestone_id = plan.id, milestones[index].id
                    reusable.scheduled_date = candidate.scheduled_date
                    reusable.estimated_minutes = reusable.reserved_minutes = candidate.estimated_minutes
                    reusable.status = previous_states[reusable.id]
                    old_tasks.remove(reusable)
            plan.status, plan.accepted_at = "active", now()
            goal.active_plan_id, goal.status = plan.id, "active"
            await dispatch(db, op, "plan_confirmed")
        op.status, op.stage = "succeeded", "saved"
        await emit(
            db,
            stream,
            "plan.activated" if accept else "plan.rejected",
            plan.id,
            "计划已确认" if accept else "计划建议已拒绝",
            goal.id,
            op.id,
            plan.version,
        )
        return serialize(plan)


async def task_action(user_id, task_id, action, payload, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        task = await owned(db, Task, task_id, user_id)
        goal = await owned(db, Goal, task.goal_id, user_id)
        op, created = await new_operation(
            db, user, stream, key, "task_action", {**payload, "task_id": task_id, "action": action}, goal
        )
        if not created:
            return serialize(task)
        require_active(goal)
        version(task.state_version, payload["expected_version"])
        if task.status not in {"ready", "in_progress", "needs_revision"}:
            raise DomainError("invalid_transition", "当前任务状态不允许此操作")
        if action == "start":
            task.status = "in_progress"
        elif action == "skip":
            await reserve(db, user, task.scheduled_date, -task.reserved_minutes)
            task.reserved_minutes, task.status = 0, "skipped"
        else:
            target = date.fromisoformat(payload["scheduled_date"])
            if target < now().astimezone(ZoneInfo(user.timezone)).date():
                raise DomainError("invalid_date", "不能改期到过去日期", 422)
            await reserve(db, user, task.scheduled_date, -task.reserved_minutes)
            await reserve(db, user, target, task.reserved_minutes)
            task.scheduled_date = target
        task.state_version += 1
        op.status, op.stage = "succeeded", "saved"
        await emit(db, stream, "task.updated", task.id, "任务安排已更新", goal.id, op.id, task.state_version)
        return serialize(task)


async def settle_study(db, user, task, payload):
    record = await db.scalar(
        select(StudyRecord).where(
            StudyRecord.user_id == user.id, StudyRecord.attempt_id == payload["attempt_id"]
        )
    )
    if record:
        if record.task_id != task.id or record.minutes != payload["minutes"]:
            raise DomainError("attempt_conflict", "该学习记录标识已用于其他内容")
        return record
    local_date = now().astimezone(ZoneInfo(user.timezone)).date()
    row = await budget(db, user, local_date)
    consumed_reservation = min(task.reserved_minutes, payload["minutes"])
    await reserve(db, user, task.scheduled_date, -consumed_reservation)
    task.reserved_minutes -= consumed_reservation
    row.consumed_minutes += payload["minutes"]
    row.version += 1
    record = StudyRecord(
        user_id=user.id,
        task_id=task.id,
        attempt_id=payload["attempt_id"],
        local_date=local_date,
        timezone=user.timezone,
        minutes=payload["minutes"],
        estimated=payload.get("estimated", False),
    )
    db.add(record)
    await db.flush()
    return record


async def study_record(user_id, task_id, payload):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        task = await owned(db, Task, task_id, user_id)
        record = await settle_study(db, user, task, payload)
        await emit(db, stream, "study.recorded", record.id, "学习投入已记录", task.goal_id)
        return serialize(record)


async def submit(user_id, task_id, payload, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        task = await owned(db, Task, task_id, user_id)
        goal = await owned(db, Goal, task.goal_id, user_id)
        op, created = await new_operation(
            db, user, stream, key, "assessment", {**payload, "task_id": task_id}, goal
        )
        if not created:
            return accepted(op, op.payload.get("submission_id"))
        require_active(goal)
        version(task.state_version, payload["expected_version"])
        if task.status in {"cancelled", "skipped", "completed"}:
            raise DomainError("invalid_transition", "此任务不再接受提交")
        for attachment_id in payload.get("attachment_ids", []):
            await owned(db, Attachment, attachment_id, user_id)
        record = None
        if payload.get("attempt_id") is not None:
            if payload.get("minutes") is None:
                raise DomainError("minutes_required", "记录学习尝试时必须提供分钟数", 422)
            record = await settle_study(db, user, task, payload)
        submission = Submission(
            user_id=user_id,
            task_id=task.id,
            text=payload["text"],
            attachment_ids=payload.get("attachment_ids", []),
            content_hash=digest({"text": payload["text"], "attachments": payload.get("attachment_ids", [])}),
            requirements_version=task.requirements_version,
            requirements_snapshot={
                "title": task.title,
                "instructions": task.instructions,
                "criteria": task.criteria,
                "skills": task.skills,
            },
            study_record_id=record.id if record else None,
        )
        db.add(submission)
        await db.flush()
        task.active_submission_id, task.status = submission.id, "awaiting_review"
        task.state_version += 1
        op.payload = {**op.payload, "submission_id": submission.id}
        op.versions = {**op.versions, "requirements_version": task.requirements_version}
        await dispatch(db, op, "submission_created")
        await emit(db, stream, "submission.saved", submission.id, "提交已保存，等待评估", goal.id, op.id)
        return accepted(op, submission.id)


async def send_message(user_id, conversation_id, payload, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        conversation = await owned(db, Conversation, conversation_id, user_id)
        goal = await owned(db, Goal, conversation.goal_id, user_id) if conversation.goal_id else None
        if goal and goal.status in {"cancelled", "completed"}:
            raise DomainError("goal_terminal", "该目标已结束，历史消息仍可查看")
        if payload.get("task_id"):
            task = await owned(db, Task, payload["task_id"], user_id)
            if goal is None or task.goal_id != goal.id:
                raise DomainError("not_found", "任务不属于当前目标", 404)
        payload = {**payload, "conversation_id": conversation.id}
        op, created = await new_operation(db, user, stream, key, "tutor", payload, goal)
        if created:
            msg = await message(
                db, stream, goal, op, payload["content"], "user", "user", payload.get("task_id")
            )
            op.payload = {**op.payload, "message_seq": msg.seq}
            source = MemorySource(
                user_id=user_id, source_id=msg.id, source_type="message", content=msg.content
            )
            db.add(source)
            await db.flush()
            await memory_outbox(db, source, "retain")
            await dispatch(db, op, "user_message" if goal else "assistant_message")
        return accepted(op)


async def memory_outbox(db, source, action):
    db.add(
        Outbox(
            user_id=source.user_id,
            kind="memory",
            dedupe_key=f"memory:{source.id}:{source.version}:{action}",
            payload={
                "type": "memory_sync",
                "user_id": source.user_id,
                "source_id": source.id,
                "version": source.version,
                "action": action,
            },
        )
    )


async def change_memory(user_id, memory_id, content=None):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        source = await owned(db, MemorySource, memory_id, user_id)
        if source.suppressed:
            if content is None:
                return {"status": "suppressed"}
            raise DomainError("memory_superseded", "此记忆已被屏蔽，请修改最新版本")
        source.suppressed, source.deleted, source.status = True, content is None, "pending"
        user.memory_control_revision += 1
        # Exclude the entire bank until synchronization/rebuild proves derived memories cannot leak.
        user.memory_recall_enabled = False
        await memory_outbox(db, source, "delete")
        if content is not None:
            new = MemorySource(
                user_id=user_id,
                source_id=source.source_id,
                source_type="correction",
                version=source.version + 1,
                content=content,
            )
            db.add(new)
            await db.flush()
            await memory_outbox(db, new, "retain")
            result = serialize(new)
        else:
            result = {"status": "suppressed"}
        await emit(db, stream, "memory.sync_changed", source.id, "记忆已在应用中屏蔽，后台同步清理")
        return result


async def edit_goal(user_id, goal_id, payload, expected_version, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        goal = await owned(db, Goal, goal_id, user_id)
        return await edit_goal_in_transaction(db, user, stream, goal, payload, expected_version, key)


async def edit_goal_in_transaction(db, user, stream, goal, payload, expected_version, key, source_operation_id=None, instruction=None):
    op, created = await new_operation(
        db,
        user,
        stream,
        key,
        "plan",
        {
            "instruction": instruction or "用户已明确修改目标，请根据新的目标重做计划，等待确认。",
            "goal_changes": payload,
            "expected_version": expected_version,
            "source_operation_id": source_operation_id,
        },
        goal,
    )
    if not created:
        return accepted(op)
    version(goal.control_revision, expected_version)
    if goal.status not in {"draft", "active"}:
        raise DomainError("goal_inactive", "请先恢复目标后再修改；已结束目标不能修改")
    if not payload:
        raise DomainError("empty_change", "请至少修改一个目标字段", 422)
    for name, value in payload.items():
        setattr(goal, name, date.fromisoformat(value) if name == "deadline" and value else value)
    if goal.active_plan_id:
        previous = await db.get(Plan, goal.active_plan_id)
        previous.status = "superseded"
    goal.status, goal.active_plan_id = "draft", None
    goal.control_revision += 1
    stale = await db.scalars(
        select(Operation).where(
            Operation.goal_id == goal.id,
            Operation.id != op.id,
            Operation.id != source_operation_id,
            Operation.status.in_(["accepted", "running", "waiting_user"]),
        )
    )
    for pending in stale:
        pending.status, pending.stage, pending.updated_at = "superseded", "superseded", now()
        await emit(
            db,
            stream,
            "operation.superseded",
            pending.id,
            "目标已修改，旧操作已停止",
            goal.id,
            pending.id,
        )
    tasks = await db.scalars(
        select(Task).where(Task.goal_id == goal.id, Task.status.in_(["ready", "needs_revision"]))
    )
    for task in tasks:
        await reserve(db, user, task.scheduled_date, -task.reserved_minutes)
        task.reserved_minutes, task.status = 0, "cancelled"
        task.state_version += 1
    op.versions = {**op.versions, "control_revision": goal.control_revision, "active_plan_id": None}
    await dispatch(db, op, "goal_created")
    await emit(
        db,
        stream,
        "goal.updated",
        goal.id,
        "目标已修改，新计划等待生成和确认",
        goal.id,
        op.id,
        goal.control_revision,
    )
    return accepted(op)
