"""Real Temporal worker tests; the provider response is replaced only inside this test process."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(not os.getenv("TEMPORAL_TEST_ADDRESS"), reason="Set TEMPORAL_TEST_ADDRESS")


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
    await Replayer(workflows=[GoalWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(await handle.fetch_history())


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
        await client.get_workflow_handle(f"assistant-{user.id}").cancel()
