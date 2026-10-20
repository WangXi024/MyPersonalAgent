from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.db.models import Conversation, DailyBudget, Evaluation, Operation, Task
from app.domain import commands, execution
from app.domain.common import DomainError


def key():
    return str(uuid4())


def plan_result():
    return {
        "kind": "plan",
        "summary": "练习函数",
        "message": "计划待确认",
        "milestones": [{"title": "函数", "criteria": ["正确使用返回值"]}],
        "tasks": [
            {
                "title": "返回值练习",
                "instructions": "写出返回值",
                "criteria": ["正确返回结果"],
                "skills": ["函数"],
                "estimated_minutes": 20,
                "scheduled_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                "milestone_index": 0,
            }
        ],
    }


async def active_goal(user):
    created = await commands.create_goal(
        user.id,
        key(),
        {"title": "Python", "background": "初学者", "outcome": "完成工具", "criteria": [], "deadline": None},
    )
    result = await execution.commit_result(created["operation_id"], plan_result())
    plan = await commands.accept_plan(user.id, result["result"]["resource_id"], 1, True, key())
    context = await execution.load_context(created["operation_id"])
    return context["goal"], context["tasks"][0], plan


async def test_create_idempotency_and_outbox(database):
    factory, user, other = database
    request_key = key()
    payload = {"title": "Python", "background": "", "outcome": "小工具", "criteria": [], "deadline": None}
    first = await commands.create_goal(user.id, request_key, payload)
    second = await commands.create_goal(user.id, request_key, payload)
    assert first == second
    with pytest.raises(DomainError, match="幂等"):
        await commands.create_goal(user.id, request_key, {**payload, "title": "different"})
    rows = await execution.claim_outbox("worker-a")
    assert len(rows) == 1
    assert await execution.claim_outbox("worker-b") == []
    await execution.ack_outbox(rows[0]["id"], "worker-b")
    await execution.ack_outbox(rows[0]["id"], "worker-a")
    assert await execution.claim_outbox("worker-a") == []


async def test_learning_evidence_and_repeated_commit(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    result = await commands.submit(
        user.id,
        task["id"],
        {
            "text": "return 3",
            "attachment_ids": [],
            "expected_version": 1,
            "attempt_id": key(),
            "minutes": 15,
            "estimated": False,
        },
        key(),
    )
    evaluation = {
        "kind": "assessment",
        "passed": True,
        "feedback": "返回值符合要求",
        "skills": [{"name": "函数", "passed": True}],
        "evidence_type": "program_verified",
    }
    first = await execution.commit_result(result["operation_id"], evaluation)
    second = await execution.commit_result(result["operation_id"], evaluation)
    assert first == second
    async with factory() as db:
        updated = await db.get(Task, task["id"])
        assert updated.status == "completed"
        row = await db.scalar(select(DailyBudget).where(DailyBudget.user_id == user.id))
        assert row.consumed_minutes == 15 and row.reserved_minutes == 0
        saved = await db.scalar(select(Evaluation))
        assert saved.evidence_type == "ai_review"  # Never trust model's invented execution claim.
        assert await db.scalar(select(func.count()).select_from(Evaluation)) == 1


async def test_pause_blocks_late_result_and_resume_does_not_revive(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    pending = await commands.request_operation(user.id, key(), goal["id"], "recommendation", {})
    paused = await commands.control_goal(user.id, goal["id"], "pause", 1, key())
    await commands.control_goal(user.id, goal["id"], "resume", paused["control_revision"], key())
    result = await execution.commit_result(
        pending["operation_id"], {"kind": "recommendation", "message": "旧推荐"}
    )
    assert result["status"] == "superseded"


async def test_out_of_order_evaluation_preserves_new_submission(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    first = await commands.submit(
        user.id, task["id"], {"text": "wrong", "attachment_ids": [], "expected_version": 1}, key()
    )
    second = await commands.submit(
        user.id, task["id"], {"text": "right", "attachment_ids": [], "expected_version": 2}, key()
    )
    await execution.commit_result(
        second["operation_id"], {"kind": "assessment", "passed": True, "feedback": "正确"}
    )
    old = await execution.commit_result(
        first["operation_id"], {"kind": "assessment", "passed": False, "feedback": "错误"}
    )
    assert old["status"] == "superseded"
    async with factory() as db:
        assert (await db.get(Task, task["id"])).status == "completed"
        assert (
            await db.scalar(select(Evaluation).where(Evaluation.operation_id == first["operation_id"]))
        ).valid is False


async def test_budget_shared_between_goals(database):
    factory, user, other = database
    await active_goal(user)
    created = await commands.create_goal(
        user.id, key(), {"title": "Second", "background": "", "outcome": "Learn"}
    )
    result = await execution.commit_result(created["operation_id"], plan_result())
    with pytest.raises(DomainError, match="超过"):
        await commands.accept_plan(user.id, result["result"]["resource_id"], 1, True, key())
    async with factory() as db:
        row = await db.scalar(select(DailyBudget).where(DailyBudget.user_id == user.id))
        assert row.reserved_minutes == 20


async def test_study_attempt_is_counted_once_even_when_failed(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    payload = {"attempt_id": key(), "minutes": 9, "estimated": False}
    await commands.study_record(user.id, task["id"], payload)
    await commands.study_record(user.id, task["id"], payload)
    async with factory() as db:
        row = await db.scalar(select(DailyBudget).where(DailyBudget.user_id == user.id))
        assert row.consumed_minutes == 9 and row.reserved_minutes == 11


async def test_user_isolation(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    with pytest.raises(DomainError) as error:
        await commands.task_action(other.id, task["id"], "skip", {"expected_version": 1}, key())
    assert error.value.status == 404


async def test_deleted_memory_fences_inflight_result(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    async with factory() as db:
        conversation = await db.scalar(select(Conversation).where(Conversation.goal_id == goal["id"]))
    op = await commands.send_message(user.id, conversation.id, {"content": "我喜欢短练习"}, key())
    context = await execution.load_context(op["operation_id"])
    await commands.change_memory(user.id, context["memories"][0]["id"])
    result = await execution.commit_result(op["operation_id"], {"kind": "tutor", "message": "旧记忆回复"})
    assert result["status"] == "superseded"
    assert (await execution.load_memory_source(context["memories"][0]["id"]))["suppressed"] is True


async def test_usage_is_snapshot_idempotent(database):
    factory, user, other = database
    created = await commands.create_goal(user.id, key(), {"title": "Python", "outcome": "Tool"})
    await execution.record_model_usage(created["operation_id"], {"requests": 2, "input_tokens": 100})
    usage = await execution.record_model_usage(created["operation_id"], {"requests": 2, "input_tokens": 100})
    assert usage == {"requests": 2, "input_tokens": 100, "output_tokens": 0}


async def test_unchanged_task_keeps_identity_when_plan_revised(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    proposed = await commands.request_operation(
        user.id, key(), goal["id"], "plan", {"instruction": "调整计划"}
    )
    result = await execution.commit_result(proposed["operation_id"], plan_result())
    accepted = await commands.accept_plan(user.id, result["result"]["resource_id"], 2, True, key())
    async with factory() as db:
        current = await db.get(Task, task["id"])
        assert current.plan_id == accepted["id"]
        assert current.status == "ready"
        assert await db.scalar(select(func.count()).select_from(Task)) == 1


async def test_memory_recall_recovers_only_after_external_cleanup(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    async with factory() as db:
        conversation = await db.scalar(select(Conversation).where(Conversation.goal_id == goal["id"]))
    operation = await commands.send_message(user.id, conversation.id, {"content": "偏好短练习"}, key())
    context = await execution.load_context(operation["operation_id"])
    source_id = context["memories"][0]["id"]
    await commands.change_memory(user.id, source_id)
    pending = await commands.send_message(user.id, conversation.id, {"content": "现在继续"}, key())
    scope = await execution.get_memory_recall_scope(pending["operation_id"])
    assert scope["enabled"] is False
    await execution.mark_memory_failed(source_id, 1)
    assert (await execution.get_memory_recall_scope(pending["operation_id"]))["enabled"] is False
    await execution.ack_memory_sync(source_id, 1)
    scope = await execution.get_memory_recall_scope(pending["operation_id"], [source_id])
    assert scope["enabled"] is True and scope["sources"] == []


async def test_cancel_goal_closes_all_queued_operations(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    queued = await commands.request_operation(user.id, key(), goal["id"], "recommendation", {})
    await commands.control_goal(user.id, goal["id"], "cancel", 1, key())
    async with factory() as db:
        assert (await db.get(Operation, queued["operation_id"])).status == "cancelled"


async def test_review_task_requires_valid_evidence_and_same_goal(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    candidate = {
        **plan_result()["tasks"][0],
        "title": "复习返回值",
        "estimated_minutes": 10,
        "review_of_task_id": task["id"],
    }
    recommendation = await commands.request_operation(user.id, key(), goal["id"], "recommendation", {})
    with pytest.raises(DomainError, match="尚无有效评估"):
        await execution.commit_result(
            recommendation["operation_id"], {"kind": "recommendation", "task": candidate}
        )
    submission = await commands.submit(
        user.id, task["id"], {"text": "return 3", "expected_version": 1}, key()
    )
    await execution.commit_result(
        submission["operation_id"], {"kind": "assessment", "passed": True, "feedback": "正确"}
    )
    result = await execution.commit_result(
        recommendation["operation_id"], {"kind": "recommendation", "task": candidate}
    )
    async with factory() as db:
        review = await db.get(Task, result["result"]["resource_id"])
        assert review.review_of_task_id == task["id"]
    second_goal, second_task, second_plan = await active_goal(user)
    cross_goal = await commands.request_operation(user.id, key(), second_goal["id"], "recommendation", {})
    with pytest.raises(DomainError, match="不属于当前目标"):
        await execution.commit_result(
            cross_goal["operation_id"], {"kind": "recommendation", "task": candidate}
        )


async def test_failed_current_evaluation_never_claims_mastery(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    submission = await commands.submit(
        user.id,
        task["id"],
        {"text": "incorrect", "expected_version": 1, "attempt_id": key(), "minutes": 7},
        key(),
    )
    await execution.commit_result(
        submission["operation_id"],
        {
            "kind": "assessment",
            "passed": False,
            "feedback": "返回值未正确使用",
            "skills": [{"name": "函数", "passed": True}],
        },
    )
    context = await execution.load_context(submission["operation_id"])
    assert context["tasks"][0]["status"] == "needs_revision"
    assert context["skill_progress"][0]["status"] == "needs_review"
    async with factory() as db:
        row = await db.scalar(select(DailyBudget).where(DailyBudget.user_id == user.id))
        assert row.consumed_minutes == 7


async def test_availability_change_fences_old_plan_result(database):
    from app.db.models import User

    factory, user, other = database
    operation = await commands.create_goal(user.id, key(), {"title": "Python", "outcome": "Tool"})
    async with factory() as db, db.begin():
        account = await db.get(User, user.id)
        account.availability_version += 1
        account.daily_minutes = 10
    result = await execution.commit_result(operation["operation_id"], plan_result())
    assert result["status"] == "superseded"


async def test_tutor_time_adjustment_creates_capped_plan_without_changing_default(database):
    from app.db.models import User

    factory, user, other = database
    goal, task, _ = await active_goal(user)
    async with factory() as db:
        conversation = await db.scalar(select(Conversation).where(Conversation.goal_id == goal["id"]))
    operation = await commands.send_message(user.id, conversation.id, {"content": "今天只有10分钟"}, key())
    result = await execution.commit_result(
        operation["operation_id"],
        {
            "kind": "tutor",
            "message": "我会准备缩短安排供你确认",
            "follow_up": {"kind": "plan", "instruction": "缩短今天任务", "today_minutes": 10},
        },
    )
    follow_id = result["result"]["follow_up_operation_id"]
    with pytest.raises(DomainError, match="超过今天剩余"):
        await execution.commit_result(follow_id, plan_result())
    proposal = plan_result()
    proposal["tasks"][0]["estimated_minutes"] = 10
    proposed = await execution.commit_result(follow_id, proposal)
    plan = await commands.accept_plan(user.id, proposed["result"]["resource_id"], 2, True, key())
    assert plan["proposal"]["remaining_today_minutes"] == 10
    async with factory() as db:
        assert (await db.get(User, user.id)).daily_minutes == 30
        assert (await db.get(Task, task["id"])).reserved_minutes == 10


async def test_edit_goal_supersedes_old_plan_and_requires_confirmation(database):
    from app.db.models import Goal, Plan

    factory, user, other = database
    goal, task, original_plan = await active_goal(user)
    edit = await commands.edit_goal(
        user.id, goal["id"], {"outcome": "新的明确成果", "deadline": None}, 1, key()
    )
    async with factory() as db:
        current = await db.get(Goal, goal["id"])
        assert current.status == "draft" and current.control_revision == 2
        assert current.outcome == "新的明确成果"
        assert (await db.get(Plan, original_plan["id"])).status == "superseded"
        assert (await db.get(Task, task["id"])).reserved_minutes == 0
    result = await execution.commit_result(edit["operation_id"], plan_result())
    await commands.accept_plan(user.id, result["result"]["resource_id"], 2, True, key())
    async with factory() as db:
        assert (await db.get(Goal, goal["id"])).status == "active"


async def test_assessment_enqueues_next_action_with_review_source(database):
    from app.db.models import Outbox, User

    factory, user, other = database
    goal, task, _ = await active_goal(user)
    async with factory() as db, db.begin():
        account = await db.get(User, user.id)
        account.quiet_start = account.quiet_end = 0
    operation = await commands.submit(user.id, task["id"], {"text": "wrong", "expected_version": 1}, key())
    result = await execution.commit_result(
        operation["operation_id"], {"kind": "assessment", "passed": False, "feedback": "需要复习"}
    )
    follow_id = result["result"]["follow_up_operation_id"]
    async with factory() as db:
        follow = await db.get(Operation, follow_id)
        assert follow.kind == "recommendation" and follow.proactive is True
        assert follow.payload["review_source_task_id"] == task["id"]
        assert await db.scalar(select(Outbox).where(Outbox.operation_id == follow_id)) is not None


async def test_weekly_review_is_a_confirmable_plan_and_obeys_contact_gap(database):
    from datetime import timedelta
    from app.db.models import Goal, User, now

    factory, user, other = database
    goal, task, _ = await active_goal(user)
    async with factory() as db, db.begin():
        account = await db.get(User, user.id)
        account.quiet_start = account.quiet_end = account.learning_hour = 0
        current = await db.get(Goal, goal["id"])
        current.created_at = now() - timedelta(days=8)
    tick = await execution.prepare_proactive(user.id, goal["id"])
    context = await execution.load_context(tick["operation_id"])
    assert context["operation"]["kind"] == "plan"
    assert context["operation"]["payload"]["trigger"] == "weekly_review"
    result = await execution.commit_result(tick["operation_id"], plan_result())
    assert result["status"] == "succeeded"
    assert (await execution.prepare_proactive(user.id, goal["id"]))["status"] == "skipped"


async def test_general_chat_isolated_without_goal_and_cannot_autocreate_goal(database):
    from app.api.routes import assistant_conversation

    factory, user, other = database
    conversation = await assistant_conversation(user)
    repeated = await assistant_conversation(user)
    assert conversation["id"] == repeated["id"] and conversation["goal_id"] is None
    op = await commands.send_message(user.id, conversation["id"], {"content": "我不知道从什么开始学"}, key())
    context = await execution.load_context(op["operation_id"])
    assert context["goal"] is None and len(context["messages"]) == 1
    assert (
        await execution.get_dispatch_state(
            {"user_id": user.id, "goal_id": None, "operation_id": op["operation_id"]}
        )
    )["goal_status"] == "general"
    assert (await execution.reserve_model_request(op["operation_id"]))["requests"] == 1
    assert (await execution.get_memory_recall_scope(op["operation_id"]))["enabled"] is True
    result = await execution.commit_result(
        op["operation_id"],
        {
            "kind": "tutor",
            "message": "我们先聊聊你的兴趣",
            "agent_history": [{"private": "user-a-only"}],
            "follow_up": {"kind": "plan", "instruction": "创建目标"},
        },
    )
    assert result["status"] == "succeeded" and "follow_up_operation_id" not in result["result"]
    other_conversation = await assistant_conversation(other)
    other_op = await commands.send_message(other.id, other_conversation["id"], {"content": "你好"}, key())
    other_context = await execution.load_context(other_op["operation_id"])
    assert other_context["agent_history"] == []
    assert all(message["content"] != "我不知道从什么开始学" for message in other_context["messages"])
    with pytest.raises(DomainError) as denied:
        await commands.send_message(other.id, conversation["id"], {"content": "越权"}, key())
    assert denied.value.status == 404


async def test_queued_chat_only_reads_its_triggering_user_turn(database):
    from app.api.routes import assistant_conversation

    factory, user, other = database
    conversation = await assistant_conversation(user)
    first_payload = {"content": "第一条问题"}
    first_key = key()
    first = await commands.send_message(user.id, conversation["id"], first_payload, first_key)
    second = await commands.send_message(user.id, conversation["id"], {"content": "第二条问题"}, key())
    assert await commands.send_message(user.id, conversation["id"], first_payload, first_key) == first
    first_context = await execution.load_context(first["operation_id"])
    assert [m["content"] for m in first_context["messages"]] == ["第一条问题"]
    await execution.commit_result(first["operation_id"], {"kind": "tutor", "message": "第一条的回复"})
    second_context = await execution.load_context(second["operation_id"])
    assert [m["content"] for m in second_context["messages"]] == ["第一条问题", "第二条问题", "第一条的回复"]


async def test_general_chat_dispatch_preserves_operation_order_including_retry(database):
    from app.api.routes import assistant_conversation, cancel_operation, retry_operation

    factory, user, other = database
    conversation = await assistant_conversation(user)
    other_conversation = await assistant_conversation(other)
    await commands.send_message(other.id, other_conversation["id"], {"content": "其他用户不应阻塞"}, key())
    first = await commands.send_message(user.id, conversation["id"], {"content": "第一条"}, key())
    second = await commands.send_message(user.id, conversation["id"], {"content": "第二条"}, key())

    async def state(operation):
        return await execution.get_dispatch_state(
            {"user_id": user.id, "goal_id": None, "operation_id": operation["operation_id"]}
        )

    assert (await state(second))["blocked_by_prior_operation"] is True
    assert (await state(first))["blocked_by_prior_operation"] is False
    await execution.mark_running(first["operation_id"])
    assert (await state(second))["blocked_by_prior_operation"] is True
    await execution.mark_failed(first["operation_id"], "provider_unavailable", "unavailable")
    assert (await state(first))["blocked_by_prior_operation"] is False
    assert (await state(second))["blocked_by_prior_operation"] is False
    retried = await retry_operation(first["operation_id"], user=user, request_key=key())
    assert retried["operation_id"] != first["operation_id"]
    assert (await state(retried))["blocked_by_prior_operation"] is True
    await execution.commit_result(second["operation_id"], {"kind": "tutor", "message": "第二条回复"})
    assert (await state(retried))["blocked_by_prior_operation"] is False
    cancelled = await commands.send_message(user.id, conversation["id"], {"content": "取消这一条"}, key())
    assert (await state(cancelled))["blocked_by_prior_operation"] is True
    await cancel_operation(cancelled["operation_id"], user=user)
    assert (await state(cancelled))["blocked_by_prior_operation"] is False


async def test_conversation_order_wait_does_not_count_as_delivery_failure(database, monkeypatch):
    from datetime import timedelta, timezone

    from app.api.routes import assistant_conversation
    from app.db.models import Outbox

    factory, user, other = database
    conversation = await assistant_conversation(user)
    await commands.send_message(user.id, conversation["id"], {"content": "你好"}, key())
    claimed = await execution.claim_outbox("dispatcher", 1)
    row_id = claimed[0]["id"]
    fixed_now = datetime.now(timezone.utc)
    monkeypatch.setattr(execution, "now", lambda: fixed_now)
    await execution.retry_outbox(row_id, "dispatcher", "ConversationOrderPending")
    async with factory() as db:
        row = await db.get(Outbox, row_id)
        assert row.status == "pending" and row.attempts == 0 and row.last_error is None
        assert row.lease_owner is None and row.lease_until is None
        assert row.next_attempt_at.replace(tzinfo=timezone.utc) == fixed_now + timedelta(seconds=1)
    monkeypatch.setattr(execution, "now", lambda: fixed_now + timedelta(seconds=1))
    assert len(await execution.claim_outbox("dispatcher", 1)) == 1
    await execution.retry_outbox(row_id, "dispatcher", "ConnectionError")
    async with factory() as db:
        row = await db.get(Outbox, row_id)
        assert row.attempts == 1 and row.last_error is not None
        assert row.next_attempt_at.replace(tzinfo=timezone.utc) == fixed_now + timedelta(seconds=3)


def goal_draft_result(**changes):
    return {
        "kind": "tutor",
        "message": "已整理为目标草稿，请修改并确认。",
        "goal_draft": {
            "title": "学习 Python",
            "background": "零基础，每天有半小时",
            "outcome": "独立制作一个文件整理工具",
            "criteria": ["能独立处理文件分类", "能解释错误处理"],
            "deadline": None,
            **changes,
        },
    }


async def pending_goal_draft(user, **changes):
    from app.api.routes import assistant_conversation

    conversation = await assistant_conversation(user)
    operation = await commands.send_message(
        user.id, conversation["id"], {"content": "帮我整理一个学习目标"}, key()
    )
    result = await execution.commit_result(operation["operation_id"], goal_draft_result(**changes))
    return conversation, result


async def test_goal_draft_only_creates_goal_after_explicit_confirmation(database):
    from app.db.models import Goal, Outbox, User

    factory, user, other = database
    conversation, source = await pending_goal_draft(user)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Goal)) == 0
        cards = await commands.goal_draft_items(db, await db.get(Conversation, conversation["id"]))
    assert cards[0]["status"] == "pending" and cards[0]["goal_id"] is None
    edits = {**cards[0]["draft"], "title": "  用户修改的目标  ", "criteria": ["制作 CSV 整理工具"]}
    request_key = key()
    response = await commands.accept_goal_draft(user.id, source["id"], request_key, edits)
    assert await commands.accept_goal_draft(user.id, source["id"], request_key, edits) == response
    replay_key = key()
    assert await commands.accept_goal_draft(user.id, source["id"], replay_key, edits) == response
    with pytest.raises(DomainError) as reused_key:
        await commands.create_goal(user.id, replay_key, {"title": "其他请求", "outcome": "另一目标"})
    assert reused_key.value.code == "idempotency_conflict"
    with pytest.raises(DomainError) as changed:
        await commands.accept_goal_draft(user.id, source["id"], key(), {**edits, "title": "其他目标"})
    assert changed.value.code == "goal_draft_conflict"
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Goal)) == 1
        goal = await db.get(Goal, response["resource_id"])
        assert goal.title == "用户修改的目标" and goal.criteria == ["制作 CSV 整理工具"]
        assert goal.status == "draft" and goal.active_plan_id is None
        assert (await db.get(User, user.id)).daily_minutes == user.daily_minutes
        tutor = await db.get(Operation, source["id"])
        assert tutor.status == "succeeded" and tutor.goal_id is None
        plan = await db.get(Operation, response["operation_id"])
        assert plan.kind == "plan" and plan.goal_id == goal.id
        outbox = await db.scalar(select(Outbox).where(Outbox.operation_id == plan.id))
        assert outbox.payload["type"] == "goal_created"
        assert await db.scalar(select(func.count()).select_from(Conversation)) == 2
        cards = await commands.goal_draft_items(db, await db.get(Conversation, conversation["id"]))
        assert cards[0]["draft"]["title"] == goal.title
        assert cards[0]["status"] == "accepted" and cards[0]["plan_operation_id"] == plan.id
    next_message = await commands.send_message(user.id, conversation["id"], {"content": "进度如何"}, key())
    context = await execution.load_context(next_message["operation_id"])
    assert context["goal_drafts"][0]["status"] == "accepted"
    assert context["goal_drafts"][0]["goal_id"] == goal.id


async def test_new_goal_draft_supersedes_only_pending_drafts(database):
    from app.db.models import Goal

    factory, user, other = database
    conversation, first = await pending_goal_draft(user)
    _, second = await pending_goal_draft(user, title="更新后的目标")
    with pytest.raises(DomainError) as superseded:
        await commands.accept_goal_draft(user.id, first["id"], key(), first["result"]["goal_draft"])
    assert superseded.value.code == "goal_draft_superseded"
    with pytest.raises(DomainError) as forbidden:
        await commands.accept_goal_draft(other.id, second["id"], key(), second["result"]["goal_draft"])
    assert forbidden.value.status == 404
    accepted = await commands.accept_goal_draft(user.id, second["id"], key(), second["result"]["goal_draft"])
    _, third = await pending_goal_draft(user, title="另一个目标草稿")
    async with factory() as db:
        cards = await commands.goal_draft_items(db, await db.get(Conversation, conversation["id"]))
        assert [card["status"] for card in cards] == ["superseded", "accepted", "pending"]
        assert cards[1]["goal_id"] == accepted["resource_id"]
        assert await db.scalar(select(func.count()).select_from(Goal)) == 1
        for card in cards:
            assert (await db.get(Operation, card["operation_id"])).status == "succeeded"
    context = await execution.load_context(third["id"])
    assert [item["status"] for item in context["goal_drafts"]] == ["superseded", "accepted", "pending"]


@pytest.mark.parametrize(
    "changes",
    [
        {"background": "  "},
        {"criteria": []},
        {"criteria": [" "]},
        {"criteria": ["x" * 501]},
        {"daily_minutes": 120},
        {"deadline": "not-a-date"},
    ],
)
async def test_invalid_goal_draft_cannot_be_persisted(database, changes):
    from app.api.routes import assistant_conversation
    from app.db.models import Goal

    factory, user, other = database
    conversation = await assistant_conversation(user)
    operation = await commands.send_message(user.id, conversation["id"], {"content": "目标"}, key())
    with pytest.raises(DomainError) as invalid:
        await execution.commit_result(operation["operation_id"], goal_draft_result(**changes))
    assert invalid.value.code == "invalid_goal_draft" and invalid.value.status == 422
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Goal)) == 0
        assert (await db.get(Operation, operation["operation_id"])).status == "accepted"
        assert (await db.get(Conversation, conversation["id"])).last_seq == 1


async def test_goal_scoped_chat_cannot_propose_new_goal(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    async with factory() as db:
        conversation = await db.scalar(select(Conversation).where(Conversation.goal_id == goal["id"]))
    operation = await commands.send_message(user.id, conversation.id, {"content": "创建另一个目标"}, key())
    with pytest.raises(DomainError) as invalid:
        await execution.commit_result(operation["operation_id"], goal_draft_result())
    assert invalid.value.code == "invalid_goal_draft"
    await execution.commit_result(operation["operation_id"], {"kind": "tutor", "message": "请在个人对话整理"})
    with pytest.raises(DomainError) as unavailable:
        await commands.accept_goal_draft(
            user.id, operation["operation_id"], key(), goal_draft_result()["goal_draft"]
        )
    assert unavailable.value.code == "goal_draft_unavailable"
    async with factory() as db:
        assert await commands.goal_draft_items(db, conversation) == []
