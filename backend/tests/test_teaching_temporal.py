"""New teaching tools cross real activities and replay, without a provider call."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("TEMPORAL_TEST_ADDRESS"), reason="Set TEMPORAL_TEST_ADDRESS")


async def test_teaching_load_history_compaction_and_components_replay(database, monkeypatch):
    from pydantic import SecretStr
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart,
    )
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.usage import RequestUsage
    from sqlalchemy import func, select
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from app.api import routes
    from app.db.models import Conversation, Evaluation, Operation, Task
    from app.domain import commands, conversation_history, execution, practice
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import OperationWorkflow
    from tests.test_domain import active_goal

    factory, user, other = database
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-not-a-provider-key"))
    goal, _, _ = await active_goal(user)
    async with factory() as db:
        conversation_id = await db.scalar(select(Conversation.id).where(Conversation.goal_id == goal["id"]))
    past = await commands.send_message(user.id, conversation_id, {"content": "复习间隔是两天 past-allowed"}, str(uuid4()))
    await execution.commit_result(past["operation_id"], {"kind": "tutor", "message": "记住你的复习安排。"})
    foreign = await routes.assistant_conversation(other)
    await commands.send_message(other.id, foreign["id"], {"content": "复习间隔 foreign-secret"}, str(uuid4()))
    current = await commands.send_message(user.id, conversation_id, {"content": "CURRENT-本次请回忆复习间隔并出一题"}, str(uuid4()))
    future = await commands.send_message(user.id, conversation_id, {"content": "复习间隔 future-secret"}, str(uuid4()))
    calls, source_reads, model_errors = [], [], []
    original_load, original_source = execution.load_context, conversation_history._source

    async def load_context(operation_id):
        context = await original_load(operation_id)
        if operation_id == current["operation_id"]:
            history = []
            for index in range(24):
                history.extend([ModelRequest(parts=[UserPromptPart(f"old-turn-{index}:" + "U" * 2000)]),
                                ModelResponse(parts=[TextPart(f"old-reply-{index}:" + "A" * 2000)])])
            context["agent_history"] = ModelMessagesTypeAdapter.dump_python(history, mode="json")
        return context

    async def source(*args, **kwargs):
        source_reads.append(1)
        return await original_source(*args, **kwargs)

    async def synthetic_request(self, messages, model_settings, model_request_parameters, **kwargs):
        parameters = model_request_parameters
        calls.append(1)
        text = str(messages)
        assert "CURRENT-本次请回忆复习间隔并出一题" in text
        assert "old-turn-0" not in text and len(messages) < 48
        names = {tool.name for tool in parameters.function_tools}
        assert "read_tool_result" not in names
        if len(calls) == 1:
            assert {"load_capability", "search_conversation_history"} <= names
            part = ToolCallPart("load_capability", {"id": "teaching-retrieval-v1"}, "teaching-load")
        elif len(calls) == 2:
            assert "先让用户回忆或预测" in text
            part = ToolCallPart("search_conversation_history", {"query": "复习间隔"}, "teaching-history")
        else:
            assert "past-allowed" in text and "future-secret" not in text and "foreign-secret" not in text
            part = ToolCallPart(parameters.output_tools[0].name, {
                "kind": "tutor", "summary": "间隔练习", "message": "你之前计划每两天复习一次，试着回忆。",
                "ui_components": [
                    {"type": "quiz", "title": "复习安排", "questions": [{"prompt": "之前的复习间隔？",
                        "choices": [{"id": "a", "text": "两天"}, {"id": "b", "text": "五天"}],
                        "correct_choice_id": "a", "explanation": "此前你明确说了两天。"}]},
                    {"type": "progress", "title": "当前记录"},
                ],
            })
        return ModelResponse(parts=[part], usage=RequestUsage(input_tokens=20, output_tokens=30), model_name="synthetic-model")

    async def request(self, messages, model_settings, model_request_parameters, **kwargs):
        try:
            return await synthetic_request(self, messages, model_settings, model_request_parameters, **kwargs)
        except Exception as error:
            model_errors.append(f"{type(error).__name__}: {error}")
            raise

    monkeypatch.setattr(execution, "load_context", load_context)
    monkeypatch.setattr(conversation_history, "_source", source)
    monkeypatch.setattr(OpenAIChatModel, "request", request)
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])
    identity = f"test-teaching-{uuid4()}"
    async with Worker(client, task_queue=identity, workflows=[OperationWorkflow], activities=ACTIVITIES):
        result = await client.execute_workflow(OperationWorkflow.run, current["operation_id"],
            id=identity, task_queue=identity, execution_timeout=timedelta(minutes=2))
    if model_errors:
        pytest.fail("\n".join(model_errors))
    assert result["status"] == "succeeded", result
    assert len(calls) == 3 and source_reads == [1]
    page = await routes.messages(conversation_id, after=0, limit=100, user=user)
    assistant = next(item for item in page["items"] if item["operation_id"] == current["operation_id"] and item["role"] == "assistant")
    quiz, progress = assistant["ui_components"]
    assert "correct_choice_id" not in str(quiz) and quiz["attempt"] is None
    assert progress["snapshot"]["tasks"]["total"] == 1
    async with factory() as db:
        op = await db.get(Operation, current["operation_id"])
        assert op.model_requests == 3 and op.result["teaching_version"] == "teaching-v1"
        assert len(op.result["teaching_pack_sha256"]) == 64
        assert len(op.result["history_calls"]) == 1
        assert "correct_choice_id" in str(op.result["private_quizzes"])
        task_before = [(task.id, task.status, task.state_version) for task in await db.scalars(select(Task))]
        assert await db.scalar(select(func.count()).select_from(Evaluation)) == 0
        assert (await db.get(Operation, future["operation_id"])).status == "accepted"
    attempt = await practice.submit_attempt(user.id, current["operation_id"], quiz["quiz_id"],
        [{"question_id": "q1", "choice_id": "a"}], str(uuid4()))
    assert attempt["correct_count"] == 1 and attempt["practice_only"] is True
    async with factory() as db:
        assert [(task.id, task.status, task.state_version) for task in await db.scalars(select(Task))] == task_before
        assert await db.scalar(select(func.count()).select_from(Evaluation)) == 0
    history = await client.get_workflow_handle(identity).fetch_history()
    scheduled = [event.activity_task_scheduled_event_attributes.activity_type.name for event in history.events
                 if event.HasField("activity_task_scheduled_event_attributes")]
    assert "agent__learning_tutor_v3__model_request" in scheduled
    assert any("learning-history-v1" in name for name in scheduled)
    await Replayer(workflows=[OperationWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
    assert len(calls) == 3 and source_reads == [1]
