import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.api import routes
from app.db.models import Goal, Message, Operation, Outbox, Plan, now
from app.domain import commands, execution
from app.domain.common import DomainError


async def setup_goal(user, title="七天学习 Temporal"):
    response = await commands.create_goal(user.id, str(uuid4()), {
        "title": title, "background": "Python 和 Docker 环境尚未确认",
        "outcome": "理解持久化执行", "criteria": ["能解释重放"],
        "deadline": (now().date() + timedelta(days=5)).isoformat(),
    })
    await execution.mark_failed(response["operation_id"], "model_timeout", "")
    return response


async def say(user, text):
    conversation = await routes.assistant_conversation(user)
    return await commands.send_message(user.id, conversation["id"], {"content": text}, str(uuid4()))


def action(goal, quote, kind="retry_plan", **kwargs):
    return {"kind": kind, "goal_id": goal["resource_id"], "expected_revision": 1,
            "request_quote": quote, **({"operation_id": goal["operation_id"]} if kind == "retry_plan" else {}),
            **kwargs}


async def reply(command, actions):
    return await execution.commit_result(command["operation_id"], {
        "kind": "tutor", "summary": "处理要求", "message": "不能用这段模型文字冒充执行结果", "actions": actions,
        "agent_history": [],  # Runtime metadata is separate from the strict provider output schema.
    })


async def test_retry_from_general_chat_is_real_idempotent_and_links_to_progress(database):
    factory, user, _ = database
    goal = await setup_goal(user)
    command = await say(user, "重试")
    result = await reply(command, [action(goal, "重试")])
    assert result == await reply(command, [action(goal, "重试")])
    receipt = result["result"]["actions"][0]
    assert receipt["status"] == "applied" and receipt["operation_id"] != goal["operation_id"]
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Operation).where(
            Operation.goal_id == goal["resource_id"], Operation.kind == "plan")) == 2
        follow = await db.get(Operation, receipt["operation_id"])
        assert follow.payload["source_operation_id"] == command["operation_id"]
        assert await db.scalar(select(Outbox).where(Outbox.operation_id == follow.id, Outbox.kind == "temporal"))
        msg = await db.scalar(select(Message).where(Message.operation_id == command["operation_id"], Message.role == "assistant"))
        assert "已开始准备" in msg.content and "冒充" not in msg.content
    repeated = await say(user, "再重试")
    second = await reply(repeated, [action(goal, "再重试")])
    assert second["result"]["actions"][0]["operation_id"] == follow.id
    context = await execution.load_context(follow.id)
    assert context["planning_request"]["user_message"] == "重试"
    overview = (await execution.load_context(repeated["operation_id"]))["goals"][0]
    assert overview["plan_count"] == 0 and overview["task_counts"] == {}
    assert overview["latest_plan_operation"]["id"] == follow.id
    conversation = await routes.assistant_conversation(user)
    page = await routes.messages(conversation["id"], after=0, limit=100, user=user)
    assert next(m for m in page["items"] if m["id"] == msg.id)["actions"] == [receipt]


async def test_confirmed_choices_update_goal_cancel_unrelated_goal_and_reach_planner(database):
    factory, user, _ = database
    goal = await setup_goal(user)
    old = await setup_goal(user, "浏览器验收 · Python 函数入门")
    command = await say(user, "Docker 可用，Python 已经会了，放弃 Python 函数入门")
    result = await reply(command, [
        action(goal, "Docker 可用，Python 已经会了", "update_goal",
               changes={"background": "已会 Python，Docker 可用；只学习 Temporal 概念。"}),
        action(old, "放弃 Python 函数入门", "cancel_goal"),
    ])
    assert all(item["status"] == "applied" for item in result["result"]["actions"])
    choice = await say(user, "使用方案二，Docker 挂在第三天")
    deadline = (now().date() + timedelta(days=6)).isoformat()
    requirements = "已会 Python，Docker 可用。学习七天，Docker 放第三天，20分钟概念+10分钟可选练习。"
    updated = await reply(choice, [action(
        goal, "使用方案二，Docker 挂在第三天", "update_goal", expected_revision=2,
        changes={"deadline": deadline, "background": requirements},
        instruction="按已确认方案二排七天；第三天的30分钟包含10分钟Docker练习。",
    )])
    receipt = updated["result"]["actions"][0]
    assert receipt["status"] == "applied"
    context = await execution.load_context(receipt["operation_id"])
    assert context["goal"]["deadline"] == deadline
    assert context["goal"]["background"] == requirements
    assert "第三天" in context["operation"]["payload"]["instruction"]
    assert context["planning_request"]["user_message"] == "使用方案二，Docker 挂在第三天"
    async with factory() as db:
        assert (await db.get(Goal, old["resource_id"])).status == "cancelled"
        assert (await db.get(Goal, goal["resource_id"])).control_revision == 3
        assert await db.scalar(select(func.count()).select_from(Plan)) == 0  # No fake generated plan.
    next_turn = await say(user, "现在怎么样")
    context = await execution.load_context(next_turn["operation_id"])
    assert [g["id"] for g in context["goals"]] == [goal["resource_id"]]
    # A manual retry of the original failed plan must also use today's saved goal fields.
    original = await execution.load_context(goal["operation_id"])
    assert original["operation"]["payload"]["background"] == requirements
    assert original["operation"]["payload"]["deadline"] == deadline


@pytest.mark.parametrize("failure", ["other_user", "stale", "missing_quote"])
async def test_rejected_action_cannot_mutate_or_claim_success(database, failure):
    factory, user, other = database
    goal = await setup_goal(other if failure == "other_user" else user)
    command = await say(user, "取消这个目标")
    change = action(goal, "取消这个目标", "cancel_goal")
    if failure == "stale":
        change["expected_revision"] = 99
    if failure == "missing_quote":
        change["request_quote"] = "旧消息要求取消"
    result = await reply(command, [change])
    receipt = result["result"]["actions"][0]
    assert receipt["status"] == "blocked" and receipt["operation_id"] is None
    if failure == "other_user":
        assert receipt["goal_id"] is None and receipt["goal_title"] == ""
    async with factory() as db:
        assert (await db.get(Goal, goal["resource_id"])).status == "draft"
        message = await db.scalar(select(Message).where(Message.operation_id == command["operation_id"],
                                                       Message.role == "assistant"))
        assert "目标已取消" not in message.content and "冒充" not in message.content
        assert await db.scalar(select(func.count()).select_from(Operation).where(Operation.kind == "control")) == 0


async def test_actions_cannot_change_settings_or_run_from_non_chat_outputs(database):
    _, user, _ = database
    goal = await setup_goal(user)
    command = await say(user, "改为第三天")
    with pytest.raises(DomainError, match="格式无效"):
        await reply(command, [action(goal, "改为第三天", "update_goal", changes={"daily_minutes": 720})])
    with pytest.raises(DomainError):
        await execution.commit_result(command["operation_id"], {
            "kind": "tutor", "message": "测试", "goal_draft": {"bad": True},
            "actions": [action(goal, "改为第三天")],
        })


async def test_context_uses_public_turns_and_keeps_future_messages_out(database):
    factory, user, _ = database
    first = await say(user, "我已经会 Python")
    await execution.commit_result(first["operation_id"], {"kind": "tutor", "message": "可以直接学习 Temporal。"})
    async with factory() as db, db.begin():
        previous = await db.get(Operation, first["operation_id"])
        previous.agent_history = [{"private_snapshot": "MUST_NOT_REPLAY_OLD_SNAPSHOT"}]
    second = await say(user, "Docker 第三天")
    await say(user, "这条更晚的消息不能提前传给旧请求")
    context = await execution.load_context(second["operation_id"])
    history = json.dumps(context["agent_history"], ensure_ascii=False)
    assert "我已经会 Python" in history and "可以直接学习 Temporal" in history
    assert "MUST_NOT_REPLAY" not in history and "更晚的消息" not in json.dumps(context, ensure_ascii=False)
    from app.agents.learning import build_prompt
    prompt = json.loads(build_prompt(context).split("\n", 1)[1])
    assert "messages" not in prompt and prompt["operation"]["payload"]["content"] == "Docker 第三天"


async def test_request_budget_allows_twenty_minutes_but_stops_at_thirty(database, monkeypatch):
    from pydantic_ai.models import ModelRequestParameters
    from temporalio.exceptions import ApplicationError

    from app.agents.learning import AGENTS
    from app.workflows.learning import model_failure_code

    _, user, _ = database
    command = await say(user, "请帮我安排")
    started = now()
    monkeypatch.setattr(execution, "now", lambda: started + timedelta(minutes=20))
    assert (await execution.reserve_model_request(command["operation_id"]))["requests"] == 1
    monkeypatch.setattr(execution, "now", lambda: started + timedelta(minutes=31))
    with pytest.raises(ApplicationError) as caught:
        await AGENTS["tutor"].model.request([], {"learning_operation_id": command["operation_id"]},
                                          ModelRequestParameters())
    assert caught.value.type == "OperationDeadline" and caught.value.non_retryable
    assert model_failure_code(caught.value) == "operation_timeout"
