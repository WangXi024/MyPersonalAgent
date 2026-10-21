"""Real Temporal worker tests; the provider response is replaced only inside this test process."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(not os.getenv("TEMPORAL_TEST_ADDRESS"), reason="Set TEMPORAL_TEST_ADDRESS")


@pytest.mark.parametrize("scenario,expected_calls", [("timeout", 2), ("recover", 2), ("authentication", 1)])
@pytest.mark.parametrize("kind", ["plan", "tutor"])
async def test_long_requests_retry_once_and_preserve_saved_content(database, monkeypatch, scenario, expected_calls, kind):
    import httpx2
    from openai import APITimeoutError
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.usage import RequestUsage
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from app.api.routes import assistant_conversation
    from app.db.models import Goal, Message, Operation, Plan
    from app.domain import commands
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import OperationWorkflow

    factory, user, _ = database
    calls = []
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-not-a-provider-key"))

    async def request(self, messages, model_settings, model_request_parameters, **kwargs):
        calls.append(model_settings["timeout"])
        if scenario == "authentication":
            raise ModelHTTPError(401, "test-model", body="private-test-payload")
        if scenario == "timeout" or len(calls) == 1:
            raise ModelAPIError("test-model", "private-timeout") from APITimeoutError(
                request=httpx2.Request("POST", "https://local-test.invalid/v1"))
        from datetime import date
        output = {"kind": "plan", "summary": "测试恢复", "message": "请确认",
                  "milestones": [{"title": "练习", "criteria": ["完成练习"]}],
                  "tasks": [{"title": "测试任务", "instructions": "完成练习", "estimated_minutes": 10,
                             "scheduled_date": date.today().isoformat(), "milestone_index": 0,
                             "criteria": ["完成练习"]}]}
        if kind == "tutor":
            output = {"kind": "tutor", "summary": "测试恢复", "message": "已准备好，可以继续学习。"}
        return ModelResponse(parts=[ToolCallPart(model_request_parameters.output_tools[0].name, output)],
                             usage=RequestUsage(input_tokens=20, output_tokens=30), model_name="test-provider")

    monkeypatch.setattr(OpenAIChatModel, "request", request)
    if kind == "plan":
        command = await commands.create_goal(user.id, str(uuid4()), {
            "title": "规划超时测试", "background": "本地测试", "outcome": "保留目标", "criteria": ["可重试"]})
    else:
        conversation = await assistant_conversation(user)
        command = await commands.send_message(user.id, conversation["id"], {"content": "请帮助我学习"}, str(uuid4()))
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])
    queue, workflow_id = f"test-timeout-{uuid4()}", f"test-timeout-{uuid4()}"
    async with Worker(client, task_queue=queue, workflows=[OperationWorkflow], activities=ACTIVITIES):
        result = await client.execute_workflow(OperationWorkflow.run, command["operation_id"],
            id=workflow_id, task_queue=queue, execution_timeout=timedelta(minutes=30))
    assert result["status"] == ("succeeded" if scenario == "recover" else "failed")
    assert calls == [600] * expected_calls
    if scenario == "timeout":
        assert result["error_code"] == "model_timeout" and "模型响应超时" in result["error_message"]
    async with factory() as db:
        assert (await db.get(Operation, command["operation_id"])).model_requests == expected_calls
        if kind == "plan":
            assert await db.get(Goal, command["resource_id"]) is not None
            plans = list(await db.scalars(select(Plan).where(Plan.goal_id == command["resource_id"])))
            assert len(plans) == (1 if scenario == "recover" else 0)
        else:
            messages = list(await db.scalars(select(Message).where(Message.conversation_id == conversation["id"])))
            assert len([message for message in messages if message.role == "user"]) == 1
            assert len([message for message in messages if message.role == "assistant"]) == (scenario == "recover")
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    scheduled = [event.activity_task_scheduled_event_attributes for event in history.events
                 if event.HasField("activity_task_scheduled_event_attributes")]
    model_activity = next(item for item in scheduled if item.activity_type.name.endswith("__model_request"))
    assert model_activity.start_to_close_timeout.seconds == 660
    assert model_activity.retry_policy.maximum_attempts == 2
    assert history.events[0].workflow_execution_started_event_attributes.workflow_execution_timeout.seconds == 1800
    await Replayer(workflows=[OperationWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
    assert len(calls) == expected_calls


async def test_durable_agent_structured_output_commits_once(tmp_path, monkeypatch):
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.usage import RequestUsage
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from app.db.models import Base, EventStream, Message, Operation, Plan, User
    from app.domain import commands, execution
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import OperationWorkflow, SmokeWorkflow

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/durable.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(commands, "SessionFactory", factory)
    monkeypatch.setattr(execution, "SessionFactory", factory)
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-not-a-provider-key"))
    requests = []

    async def model_request(self, messages, model_settings, model_request_parameters, **kwargs):
        requests.append(messages)
        # The model must emit the actual registered structured output tool name.
        tool = model_request_parameters.output_tools[0]
        from datetime import date

        output = {
            "kind": "plan",
            "summary": "理解函数并完成练习",
            "message": "计划已准备好，请确认。",
            "milestones": [{"title": "理解函数", "criteria": ["能解释输入输出"]}],
            "tasks": [
                {
                    "title": "实现求和函数",
                    "instructions": "实现函数并解释边界条件",
                    "estimated_minutes": 15,
                    "scheduled_date": date.today().isoformat(),
                    "milestone_index": 0,
                    "criteria": ["能解释输入输出"],
                    "skills": ["函数"],
                }
            ],
        }
        return ModelResponse(
            parts=[ToolCallPart(tool.name, output)],
            usage=RequestUsage(input_tokens=20, output_tokens=30),
            model_name="test-provider",
        )

    monkeypatch.setattr(OpenAIChatModel, "request", model_request)
    async with factory() as db, db.begin():
        user = User(username=f"durable-{uuid4()}", password_hash="not-used")
        db.add(user)
        await db.flush()
        db.add(EventStream(user_id=user.id))
        user_id = user.id
    command = await commands.create_goal(
        user_id,
        str(uuid4()),
        {
            "title": "学习 Python",
            "background": "初学者",
            "outcome": "能编写基础函数",
            "criteria": ["能解释输入输出"],
        },
    )
    client = await Client.connect(
        os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"),
        plugins=[PydanticAIPlugin()],
    )
    queue = f"test-runtime-{uuid4()}"
    workflow_id = f"test-operation-{uuid4()}"
    try:
        async with Worker(
            client, task_queue=queue, workflows=[OperationWorkflow, SmokeWorkflow], activities=ACTIVITIES
        ):
            assert (
                await client.execute_workflow(
                    SmokeWorkflow.run,
                    "ok",
                    id=f"test-smoke-{uuid4()}",
                    task_queue=queue,
                    execution_timeout=timedelta(seconds=30),
                )
                == "ok"
            )
            result = await client.execute_workflow(
                OperationWorkflow.run,
                command["operation_id"],
                id=workflow_id,
                task_queue=queue,
                execution_timeout=timedelta(seconds=60),
            )
            assert result["status"] == "succeeded", result
        history = await client.get_workflow_handle(workflow_id).fetch_history()
        await Replayer(workflows=[OperationWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
        async with factory() as db:
            plans = list((await db.scalars(select(Plan))).all())
            messages = list((await db.scalars(select(Message))).all())
            operation = await db.get(Operation, command["operation_id"])
            assert len(plans) == 1 and len(messages) == 1
            assert operation.agent_history
            assert operation.model_requests == 1
        assert len(requests) == 1  # Replay does not contact the model again.
        repeated = await execution.commit_result(command["operation_id"], {"kind": "plan"})
        assert repeated["status"] == "succeeded"
    finally:
        await engine.dispose()


async def test_goal_cancel_stops_child_and_late_outbox_is_harmless(database, monkeypatch):
    import asyncio

    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.models.openai import OpenAIChatModel
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from app.db.models import Operation, Plan
    from app.dispatcher import deliver
    from app.domain import commands
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import GoalWorkflow, OperationWorkflow

    factory, user, _ = database
    started = asyncio.Event()

    async def pending_request(self, *args, **kwargs):
        started.set()
        await asyncio.sleep(120)
        raise AssertionError("Cancelled provider call must not produce output")

    monkeypatch.setattr(OpenAIChatModel, "request", pending_request)
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-not-a-provider-key"))
    command = await commands.create_goal(user.id, str(uuid4()), {
        "title": "取消验证", "background": "本地测试", "outcome": "不会发布迟到结果", "criteria": ["无迟到计划"]})
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])
    queue = f"test-cancel-{uuid4()}"
    goal_id = command["resource_id"]
    workflow_id = f"goal-{user.id}-{goal_id}"
    payload = {"type": "goal_created", "goal_id": goal_id, "user_id": user.id,
               "operation_id": command["operation_id"], "command_id": str(uuid4())}
    async with Worker(client, task_queue=queue, workflows=[GoalWorkflow, OperationWorkflow], activities=ACTIVITIES):
        handle = await client.start_workflow(GoalWorkflow.run,
            {"goal_id": goal_id, "user_id": user.id, "pending": [payload]},
            id=workflow_id, task_queue=queue, execution_timeout=timedelta(seconds=60))
        await asyncio.wait_for(started.wait(), 15)
        await commands.control_goal(user.id, goal_id, "cancel", 1, str(uuid4()))
        await handle.signal(GoalWorkflow.command, {"type": "goal_cancelled", "command_id": str(uuid4())})
        await asyncio.wait_for(handle.result(), 15)
        async with factory() as db:
            assert (await db.get(Operation, command["operation_id"])).status == "cancelled"
            assert await db.scalar(select(Plan).where(Plan.goal_id == goal_id)) is None
        # A delayed command must be acknowledged without recreating the closed supervisor.
        await deliver(client, {"kind": "temporal", "id": str(uuid4()), "payload": payload})
        await deliver(client, {"kind": "temporal", "id": str(uuid4()),
            "payload": {**payload, "type": "goal_cancelled"}})
    history = await handle.fetch_history()
    children = [event.start_child_workflow_execution_initiated_event_attributes for event in history.events
                if event.HasField("start_child_workflow_execution_initiated_event_attributes")]
    assert children[0].workflow_execution_timeout.seconds == 1800
    await Replayer(workflows=[GoalWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)


async def test_personal_chat_uses_durable_serial_inbox_without_creating_a_goal(database, monkeypatch):
    import asyncio

    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.usage import RequestUsage
    from temporalio.client import Client
    from temporalio.worker import Worker

    from app.db.models import Conversation, Goal, Message, Operation
    from app.dispatcher import ConversationOrderPending, deliver
    from app.domain import commands
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import AssistantWorkflow, OperationWorkflow

    factory, user, _ = database
    async with factory() as db, db.begin():
        conversation = Conversation(user_id=user.id, goal_id=None)
        db.add(conversation)
        await db.flush()
    first = await commands.send_message(user.id, conversation.id, {"content": "我想学一门新技能"}, str(uuid4()))
    second = await commands.send_message(user.id, conversation.id, {"content": "但还没确定方向"}, str(uuid4()))
    calls = []

    async def reply(self, messages, model_settings, model_request_parameters, **kwargs):
        calls.append(messages)
        return ModelResponse(parts=[ToolCallPart(model_request_parameters.output_tools[0].name,
            {"kind": "tutor", "summary": "明确兴趣", "message": "可以先聊聊你感兴趣的方向。", "follow_up": None})],
            usage=RequestUsage(input_tokens=10, output_tokens=10), model_name="test-provider")

    monkeypatch.setattr(OpenAIChatModel, "request", reply)
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-not-a-provider-key"))
    queue = f"test-personal-{uuid4()}"
    monkeypatch.setattr(runtime_settings(), "temporal_task_queue", queue)
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])
    async with Worker(client, task_queue=queue, workflows=[AssistantWorkflow, OperationWorkflow], activities=ACTIVITIES):
        async def completed(operation_id):
            while True:
                async with factory() as db:
                    operation = await db.get(Operation, operation_id)
                    if operation.status in {"failed", "succeeded"}:
                        assert operation.status == "succeeded"
                        return
                await asyncio.sleep(0.1)

        items = [{"kind": "temporal", "id": str(uuid4()), "payload": {"type": "assistant_message",
            "user_id": user.id, "goal_id": None, "operation_id": command["operation_id"]}}
            for command in (first, second)]
        # Failed earlier delivery or concurrent dispatchers must not reverse conversation order.
        with pytest.raises(ConversationOrderPending):
            await deliver(client, items[1])
        for item in items:
            await deliver(client, item)
            await deliver(client, item)  # Outbox acknowledgement loss must not duplicate the response.
            await asyncio.wait_for(completed(item["payload"]["operation_id"]), 20)
        async with factory() as db:
            assert await db.scalar(select(Goal).where(Goal.user_id == user.id)) is None
            messages = list(await db.scalars(select(Message).where(Message.conversation_id == conversation.id)))
            assert len(messages) == 4
            assert len([m for m in messages if m.role == "assistant"]) == 2
        assert len(calls) == 2
        assert len(calls[1]) > len(calls[0])  # The canonical previous turn is continued.
        handle = client.get_workflow_handle(f"assistant-{user.id}")
        history = await handle.fetch_history()
        children = [event.start_child_workflow_execution_initiated_event_attributes for event in history.events
                    if event.HasField("start_child_workflow_execution_initiated_event_attributes")]
        assert len(children) == 2 and all(child.workflow_execution_timeout.seconds == 1800 for child in children)
        await handle.cancel()


async def test_chat_clarifies_then_drafts_and_only_confirmation_starts_planning(database, monkeypatch):
    import asyncio
    from datetime import date

    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.usage import RequestUsage
    from temporalio.client import Client
    from temporalio.worker import Worker

    from app.api.routes import assistant_conversation, goal_drafts
    from app.db.models import Goal, Operation, Outbox, Plan, Task
    from app.dispatcher import deliver
    from app.domain import commands
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import AssistantWorkflow, GoalWorkflow, OperationWorkflow

    factory, user, _ = database
    conversation = await assistant_conversation(user)
    draft = {"title": "学习 Python", "background": "零基础", "outcome": "完成求和小工具",
             "criteria": ["能解释函数输入输出"], "deadline": None}
    calls = []

    async def respond(self, messages, model_settings, model_request_parameters, **kwargs):
        tool = model_request_parameters.output_tools[0]
        planning = "milestones" in tool.parameters_json_schema.get("properties", {})
        calls.append("plan" if planning else "tutor")
        if planning:
            output = {"kind": "plan", "summary": "先掌握函数", "message": "计划已准备，请确认。",
                "milestones": [{"title": "函数", "criteria": ["能解释函数输入输出"]}],
                "tasks": [{"title": "实现求和函数", "instructions": "实现并解释输入输出",
                    "estimated_minutes": 15, "scheduled_date": date.today().isoformat(),
                    "milestone_index": 0, "criteria": ["能解释函数输入输出"], "skills": ["函数"]}]}
        else:
            output = {"kind": "tutor", "summary": "整理目标", "follow_up": None,
                "message": "你现在有什么基础？" if len(calls) == 1 else "请检查下方草稿并确认创建。",
                "goal_draft": None if len(calls) == 1 else draft}
        return ModelResponse(parts=[ToolCallPart(tool.name, output)],
            usage=RequestUsage(input_tokens=30, output_tokens=50), model_name="test-provider")

    monkeypatch.setattr(OpenAIChatModel, "request", respond)
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-not-a-provider-key"))
    queue = f"test-chat-goal-{uuid4()}"
    monkeypatch.setattr(runtime_settings(), "temporal_task_queue", queue)
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])

    async def dispatch_and_finish(operation_id):
        async with factory() as db:
            item = await db.scalar(select(Outbox).where(Outbox.operation_id == operation_id,
                                                       Outbox.kind == "temporal"))
            command = {"id": item.id, "kind": item.kind, "payload": item.payload}
        await deliver(client, command)
        for _ in range(200):
            async with factory() as db:
                operation = await db.get(Operation, operation_id)
                if operation.status in {"succeeded", "failed", "superseded"}:
                    assert operation.status == "succeeded", operation.error_code
                    return
            await asyncio.sleep(0.1)
        raise AssertionError("Operation did not finish")

    goal_id = None
    async with Worker(client, task_queue=queue, workflows=[AssistantWorkflow, GoalWorkflow, OperationWorkflow],
                      activities=ACTIVITIES):
        try:
            first = await commands.send_message(user.id, conversation["id"],
                {"content": "我想学习 Python"}, str(uuid4()))
            await dispatch_and_finish(first["operation_id"])
            assert (await goal_drafts(conversation["id"], user))["items"] == []
            second = await commands.send_message(user.id, conversation["id"],
                {"content": "零基础，希望完成求和小工具，能解释输入输出。"}, str(uuid4()))
            await dispatch_and_finish(second["operation_id"])
            cards = (await goal_drafts(conversation["id"], user))["items"]
            assert len(cards) == 1 and cards[0]["status"] == "pending"
            async with factory() as db:
                assert await db.scalar(select(Goal).where(Goal.user_id == user.id)) is None
            edited = {**draft, "title": "我的第一个 Python 工具"}
            accepted = await commands.accept_goal_draft(user.id, second["operation_id"], str(uuid4()), edited)
            goal_id = accepted["resource_id"]
            assert await commands.accept_goal_draft(user.id, second["operation_id"], str(uuid4()), edited) == accepted
            await dispatch_and_finish(accepted["operation_id"])
            async with factory() as db:
                goal = await db.get(Goal, goal_id)
                assert goal.title == edited["title"] and goal.status == "draft"
                plan = await db.scalar(select(Plan).where(Plan.goal_id == goal_id))
                assert plan.status == "proposed"
                assert await db.scalar(select(Task).where(Task.goal_id == goal_id)) is None
                assert (await db.get(Operation, second["operation_id"])).goal_id is None
            cards = (await goal_drafts(conversation["id"], user))["items"]
            assert cards[0]["status"] == "accepted" and cards[0]["goal_id"] == goal_id
            assert calls == ["tutor", "tutor", "plan"]
        finally:
            workflow_ids = [f"assistant-{user.id}"] + ([f"goal-{user.id}-{goal_id}"] if goal_id else [])
            for workflow_id in workflow_ids:
                await client.get_workflow_handle(workflow_id).cancel()


async def test_chat_choice_reaches_durable_planner_and_stays_pending_until_accepted(database, monkeypatch):
    import asyncio
    import json
    from zoneinfo import ZoneInfo

    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.usage import RequestUsage
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from app.api.routes import assistant_conversation
    from app.db.models import Goal, Operation, Outbox, Plan, Task, now
    from app.dispatcher import deliver
    from app.domain import commands, execution
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import AssistantWorkflow, GoalWorkflow, OperationWorkflow

    factory, user, _ = database
    start = now().astimezone(ZoneInfo(user.timezone)).date()
    deadline = (start + timedelta(days=6)).isoformat()
    background = f"已会 Python，Docker 可用。{start} 开始七天，每天30分钟；第三天20分钟概念+10分钟可选Docker。"
    goal = await commands.create_goal(user.id, str(uuid4()), {
        "title": "七天学习 Temporal", "background": "基础和环境待确认", "outcome": "理解持久化执行",
        "criteria": ["解释重放与 signal"], "deadline": (start + timedelta(days=5)).isoformat()})
    await execution.mark_failed(goal["operation_id"], "model_timeout", "")
    conversation = await assistant_conversation(user)
    choice = "使用方案二，Docker 挂在第三天"
    calls = []

    async def respond(self, messages, model_settings, model_request_parameters, **kwargs):
        tool = model_request_parameters.output_tools[0]
        planning = "milestones" in tool.parameters_json_schema.get("properties", {})
        calls.append("plan" if planning else "tutor")
        if planning:
            prompt = next(part.content for msg in reversed(messages) for part in msg.parts
                          if isinstance(part, UserPromptPart) and isinstance(part.content, str)
                          and part.content.startswith("请根据"))
            context = json.loads(prompt.split("\n", 1)[1])
            assert context["goal"]["deadline"] == deadline and context["goal"]["background"] == background
            assert context["planning_request"]["user_message"] == choice
            tasks = [{"title": f"第{day + 1}天", "instructions": "理解概念并主动回忆",
                      "estimated_minutes": 20 if day == 2 else 30,
                      "scheduled_date": (start + timedelta(days=day)).isoformat(),
                      "milestone_index": 0, "criteria": ["解释所学概念"]} for day in range(7)]
            tasks.append({**tasks[2], "title": "可选 Docker 练习", "estimated_minutes": 10})
            output = {"kind": "plan", "summary": "七天理解型计划", "message": "请检查并确认。",
                      "milestones": [{"title": "理解持久化执行", "criteria": ["解释重放与 signal"]}],
                      "tasks": tasks}
        else:
            output = {"kind": "tutor", "summary": "执行已确认选择", "message": "不应发布模型自行声称的执行结果",
                      "actions": [{"kind": "update_goal", "goal_id": goal["resource_id"], "expected_revision": 1,
                                   "request_quote": choice, "changes": {"background": background, "deadline": deadline}}]}
        return ModelResponse(parts=[ToolCallPart(tool.name, output)],
                             usage=RequestUsage(input_tokens=30, output_tokens=50), model_name="test-provider")

    monkeypatch.setattr(OpenAIChatModel, "request", respond)
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-not-a-provider-key"))
    queue = f"test-chat-action-{uuid4()}"
    monkeypatch.setattr(runtime_settings(), "temporal_task_queue", queue)
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])

    parents = set()
    async def finish(operation_id):
        async with factory() as db:
            item = await db.scalar(select(Outbox).where(Outbox.operation_id == operation_id, Outbox.kind == "temporal"))
            command = {"id": item.id, "kind": item.kind, "payload": item.payload}
        await deliver(client, command)
        parents.add(f"goal-{user.id}-{command['payload']['goal_id']}" if command["payload"]["goal_id"]
                    else f"assistant-{user.id}")
        await deliver(client, command)  # Redelivery must not repeat the command or model call.
        for _ in range(200):
            async with factory() as db:
                operation = await db.get(Operation, operation_id)
                if operation.status in {"succeeded", "failed", "superseded"}:
                    assert operation.status == "succeeded", operation.error_code
                    return operation.result
            await asyncio.sleep(0.1)
        raise AssertionError("Operation did not finish")

    async with Worker(client, task_queue=queue, workflows=[AssistantWorkflow, GoalWorkflow, OperationWorkflow],
                      activities=ACTIVITIES):
        try:
            sent = await commands.send_message(user.id, conversation["id"], {"content": choice}, str(uuid4()))
            result = await finish(sent["operation_id"])
            follow_id = result["actions"][0]["operation_id"]
            assert result["actions"][0]["status"] == "applied"
            await finish(follow_id)
            async with factory() as db:
                saved = await db.get(Goal, goal["resource_id"])
                assert saved.control_revision == 2 and saved.status == "draft"
                plan = await db.scalar(select(Plan).where(Plan.goal_id == saved.id))
                assert plan.status == "proposed"
                assert await db.scalar(select(Task).where(Task.goal_id == saved.id)) is None
            for operation_id in (sent["operation_id"], follow_id):
                history = await client.get_workflow_handle(f"operation-{operation_id}").fetch_history()
                await Replayer(workflows=[OperationWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
            assert calls == ["tutor", "plan"]
            await commands.accept_plan(user.id, plan.id, plan.version, True, str(uuid4()))
            async with factory() as db:
                tasks = list(await db.scalars(select(Task).where(Task.goal_id == saved.id)))
                by_date = {}
                for task in tasks:
                    by_date[task.scheduled_date] = by_date.get(task.scheduled_date, 0) + task.estimated_minutes
                assert len(tasks) == 8 and len(by_date) == 7
                assert min(by_date) == start and max(by_date).isoformat() == deadline
                assert set(by_date.values()) == {30}
        finally:
            for workflow_id in parents:
                await client.get_workflow_handle(workflow_id).cancel()


@pytest.mark.parametrize("scenario", ["old_quote", "cancelled_target", "exhausted"])
async def test_cancelled_goal_chat_corrects_output_before_committing(database, monkeypatch, scenario):
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, UserPromptPart
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.usage import RequestUsage
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from app.api.routes import assistant_conversation
    from app.db.models import Goal, Message, Operation, Plan
    from app.domain import commands, execution
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import OperationWorkflow

    factory, user, _ = database
    conversation = await assistant_conversation(user)
    draft = {"title": "七天学习 Temporal", "background": "已会 Python，Docker 可用，每天30分钟",
             "outcome": "理解持久化执行", "criteria": ["解释重放与 signal"], "deadline": None}
    old_message = await commands.send_message(user.id, conversation["id"],
        {"content": "使用方案二，Docker 挂在第三天"}, str(uuid4()))
    await execution.commit_result(old_message["operation_id"], {"kind": "tutor", "message": "请确认旧草稿", "goal_draft": draft})
    old = await commands.accept_goal_draft(user.id, old_message["operation_id"], str(uuid4()), draft)
    await commands.control_goal(user.id, old["resource_id"], "cancel", 1, str(uuid4()))
    content = "我想用七天学习一下 Temporal 持久化框架"
    command = await commands.send_message(user.id, conversation["id"], {"content": content}, str(uuid4()))
    calls = []

    async def respond(self, messages, model_settings, model_request_parameters, **kwargs):
        import json

        calls.append(messages)
        if len(calls) == 1:
            prompt = next(p.content for m in reversed(messages) for p in m.parts
                          if isinstance(p, UserPromptPart) and isinstance(p.content, str) and p.content.startswith("请根据"))
            snapshot = json.loads(prompt.split("\n", 1)[1])
            assert snapshot["chat_request"]["content"] == content
            assert snapshot["goal_drafts"][0]["goal_status"] == "cancelled"
        else:
            assert any(isinstance(p, RetryPromptPart) for m in messages for p in m.parts)
        if len(calls) == 1 or scenario == "exhausted":
            output = {"kind": "tutor", "summary": "错误操作", "message": "这段错误答复不能发布",
                      "actions": [{"kind": "update_goal", "goal_id": old["resource_id"], "expected_revision": 2,
                                   "request_quote": content if scenario == "cancelled_target" else "使用方案二，Docker 挂在第三天",
                                   "changes": {"background": "不应写入旧目标"}}]}
        else:
            output = {"kind": "tutor", "summary": "重新整理目标", "message": "旧目标已取消，请检查这份新草稿。",
                      "goal_draft": draft}
        return ModelResponse(parts=[ToolCallPart(model_request_parameters.output_tools[0].name, output)],
                             usage=RequestUsage(input_tokens=30, output_tokens=50), model_name="test-provider")

    monkeypatch.setattr(OpenAIChatModel, "request", respond)
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-not-a-provider-key"))
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])
    queue, workflow_id = f"test-cancelled-chat-{uuid4()}", f"test-cancelled-chat-{uuid4()}"
    async with Worker(client, task_queue=queue, workflows=[OperationWorkflow], activities=ACTIVITIES):
        result = await client.execute_workflow(OperationWorkflow.run, command["operation_id"],
            id=workflow_id, task_queue=queue, execution_timeout=timedelta(minutes=30))
    if scenario == "exhausted":
        assert len(calls) == 3 and result["status"] == "failed"
        assert result["error_code"] == "invalid_chat_action"
        assert "Agent 未能正确处理你这条消息" in result["error_message"]
    else:
        assert len(calls) == 2 and result["status"] == "succeeded"
        assert result["result"]["goal_draft_status"] == "pending"
    async with factory() as db:
        goals = list(await db.scalars(select(Goal)))
        assert len(goals) == 1 and goals[0].status == "cancelled" and goals[0].background == draft["background"]
        assert await db.scalar(select(Plan)) is None
        assert (await db.get(Operation, command["operation_id"])).model_requests == len(calls)
        published = list(await db.scalars(select(Message).where(
            Message.operation_id == command["operation_id"], Message.role == "assistant")))
        assert len(published) == (0 if scenario == "exhausted" else 1)
        assert all("错误答复" not in m.content for m in published)
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    await Replayer(workflows=[OperationWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
    assert len(calls) == (3 if scenario == "exhausted" else 2)
