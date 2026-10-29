"""Real Temporal activity/replay checks, without a paid model or web request."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("TEMPORAL_TEST_ADDRESS"), reason="Set TEMPORAL_TEST_ADDRESS")


@pytest.mark.parametrize("legacy", [False, True])
async def test_research_ui_activities_and_legacy_replay(database, monkeypatch, legacy):
    from pydantic import SecretStr
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.usage import RequestUsage
    from temporalio import workflow
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from app.api import routes
    from app.db.models import Operation
    from app.domain import commands, research
    from app.integrations.settings import runtime_settings
    from app.workflows.activities import ACTIVITIES
    from app.workflows.learning import OperationWorkflow

    factory, user, _ = database
    monkeypatch.setattr(research, "SessionFactory", factory)
    monkeypatch.setattr(runtime_settings(), "research_enabled", True)
    monkeypatch.setattr(runtime_settings(), "litellm_api_key", SecretStr("test-only-no-provider-call"))
    requests, searches = [], []
    source = {"id": "src_test", "title": "Python 教程", "url": "https://docs.python.org/3/tutorial/",
              "snippet": "官方教程", "retrieved_at": "2026-10-29T01:00:00+00:00"}

    async def search(query):
        searches.append(query)
        return [source]

    async def request(self, messages, model_settings, model_request_parameters, **kwargs):
        requests.append(1)
        if not legacy and len(requests) == 1:
            names = {tool.name for tool in model_request_parameters.function_tools}
            assert "search_learning_resources" in names
            part = ToolCallPart("search_learning_resources", {"query": "Python tutorial"}, "research-call-1")
        else:
            output = {"kind": "tutor", "summary": "参考资料", "message": "请阅读教程。"}
            if not legacy:
                output["ui_components"] = [{"type": "resource_list", "title": "官方资料", "source_ids": [source["id"]]}]
            part = ToolCallPart(model_request_parameters.output_tools[0].name, output)
        return ModelResponse(parts=[part], usage=RequestUsage(input_tokens=20, output_tokens=30), model_name="test-model")

    monkeypatch.setattr(research.web, "search_web", search)
    monkeypatch.setattr(OpenAIChatModel, "request", request)
    original_patched = workflow.patched
    if legacy:
        monkeypatch.setattr(workflow, "patched", lambda marker: False if marker == "learning-research-ui-v1"
                            else original_patched(marker))
    conversation = await routes.assistant_conversation(user)
    command = await commands.send_message(user.id, conversation["id"], {
        "content": "查找 Python 官方教程", "research_requested": True,
    }, str(uuid4()))
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])
    identity = f"test-research-{uuid4()}"
    async with Worker(client, task_queue=identity, workflows=[OperationWorkflow], activities=ACTIVITIES):
        result = await client.execute_workflow(OperationWorkflow.run, command["operation_id"],
            id=identity, task_queue=identity, execution_timeout=timedelta(minutes=2))
    assert result["status"] == "succeeded", result
    assert len(requests) == (1 if legacy else 2)
    assert searches == ([] if legacy else ["Python tutorial"])
    page = await routes.messages(conversation["id"], after=0, limit=100, user=user)
    assert len(page["items"]) == 2
    if not legacy:
        assert page["items"][1]["ui_components"][0]["items"] == [source]
        async with factory() as db:
            saved = await db.get(Operation, command["operation_id"])
            assert saved.result["research_calls"][0]["status"] == "done"
    history = await client.get_workflow_handle(identity).fetch_history()
    monkeypatch.setattr(workflow, "patched", original_patched)
    await Replayer(workflows=[OperationWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
    assert len(requests) == (1 if legacy else 2)
    assert len(searches) == (0 if legacy else 1)
