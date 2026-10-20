import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.api import routes
from app.db.models import BusinessEvent, Conversation, Operation, Outbox
from app.domain import commands, execution
from app.domain.common import DomainError
from app.integrations import model_catalog
from app.integrations.settings import RuntimeSettings
from app.main import app


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    models = [
        {"id": "learning-agent", "name": "test-primary", "provider": "bailian"},
        {"id": "learning-fallback", "name": "test-secondary", "provider": "zhipu"},
    ]
    path.write_text(json.dumps(models))
    monkeypatch.setattr(model_catalog, "runtime_settings", lambda: RuntimeSettings(chat_model_catalog_path=path))
    return path, models


async def test_model_selection_api_is_private_versioned_and_does_not_enqueue_work(database, catalog):
    factory, user, other = database
    conversation = await routes.assistant_conversation(user)
    other_conversation = await routes.assistant_conversation(other)
    path = f"/api/v1/conversations/{conversation['id']}/model"
    other_path = f"/api/v1/conversations/{other_conversation['id']}/model"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        assert (await client.get(path)).status_code == 401
        login = await client.post("/api/v1/auth/login",
            json={"username": "learner", "password": "test-only-long-password"},
            headers={"Origin": "http://localhost:8080"})
        headers = {"X-CSRF-Token": login.json()["csrf_token"], "Origin": "http://localhost:8080"}
        payload = {"model_id": "learning-fallback", "expected_version": 1}
        listing = (await client.get(path)).json()
        assert listing == {"items": catalog[1], "model_id": "learning-agent", "version": 1}
        assert (await client.patch(path, json=payload)).status_code == 403
        assert (await client.get(other_path)).status_code == 404
        assert (await client.patch(other_path, json=payload, headers=headers)).status_code == 404
        assert (await client.patch(path, json={**payload, "model_id": "arbitrary-model"}, headers=headers)).status_code == 409
        selected = await client.patch(path, json=payload, headers=headers)
        assert selected.status_code == 200
        assert selected.json()["version"] == 2
        assert (await client.patch(path, json=payload, headers=headers)).json() == selected.json()
        assert (await client.get(path)).json() == selected.json()
        stale = await client.patch(path, json={**payload, "model_id": "learning-agent"}, headers=headers)
        assert stale.status_code == 409
        assert (await client.get(other_path)).status_code == 404
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Operation)) == 0
        assert await db.scalar(select(func.count()).select_from(Outbox)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessEvent).where(
            BusinessEvent.type == "conversation.model_changed")) == 1
        assert (await db.get(Conversation, other_conversation["id"])).model_alias is None


async def test_requests_snapshot_the_model_and_retries_keep_the_original_snapshot(database, catalog):
    factory, user, other = database
    conversation = await routes.assistant_conversation(user)
    first = await commands.send_message(user.id, conversation["id"], {"content": "first"}, "first-message")
    await routes.select_conversation_model(conversation["id"],
        routes.ConversationModelInput(model_id="learning-fallback", expected_version=1), user)
    second = await commands.send_message(user.id, conversation["id"], {"content": "second"}, "second-message")
    repeated = await commands.send_message(user.id, conversation["id"], {"content": "first"}, "first-message")
    assert repeated["operation_id"] == first["operation_id"]
    assert (await execution.reserve_model_request(first["operation_id"]))["model_alias"] == "learning-agent"
    assert (await execution.reserve_model_request(second["operation_id"]))["model_alias"] == "learning-fallback"
    # Subsequent model attempts for an already submitted operation keep its selected alias.
    await routes.select_conversation_model(conversation["id"],
        routes.ConversationModelInput(model_id="learning-agent", expected_version=2), user)
    assert (await execution.reserve_model_request(second["operation_id"]))["model_alias"] == "learning-fallback"
    catalog[0].write_text(json.dumps(catalog[1][:1]))
    with pytest.raises(DomainError, match="所选模型已不可用"):
        await execution.reserve_model_request(second["operation_id"])
    async with factory() as db:
        assert (await db.get(Operation, second["operation_id"])).model_requests == 2


def test_catalog_exposes_only_display_metadata_and_fails_closed(catalog):
    path, models = catalog
    path.write_text(json.dumps([{**models[0], "api_key": "test-only-secret-field"}]))
    assert model_catalog.configured_models() == models[:1]
    path.write_text("invalid json")
    with pytest.raises(DomainError) as caught:
        model_catalog.configured_models()
    assert caught.value.status == 503
    assert caught.value.message == "模型列表暂不可用，请稍后重试"


async def test_concurrent_model_calls_do_not_mutate_the_shared_agent(monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.openai import OpenAIChatModel

    from app.agents.learning import AGENTS

    model = AGENTS["tutor"].model
    original = model.wrapped.model_name
    routed = []
    recorded = []

    async def reserve(operation_id, limit):
        await asyncio.sleep(0)
        return {"model_alias": "learning-fallback" if operation_id == "second" else "learning-agent"}

    async def request(self, messages, model_settings, parameters):
        routed.append((self.model_name, self.provider.client))
        assert "learning_operation_id" not in model_settings
        return ModelResponse(parts=[TextPart("test response")])

    async def record(operation_id, input_tokens, output_tokens):
        recorded.append(operation_id)

    monkeypatch.setattr(execution, "reserve_model_request", reserve)
    monkeypatch.setattr(execution, "record_model_tokens", record)
    monkeypatch.setattr(OpenAIChatModel, "request", request)
    await asyncio.gather(*(model.request([], {"learning_operation_id": name}, ModelRequestParameters())
                           for name in ("first", "second")))
    assert {alias for alias, _ in routed} == {"learning-agent", "learning-fallback"}
    assert all(client is model.wrapped.provider.client for _, client in routed)
    assert set(recorded) == {"first", "second"}
    assert model.wrapped.model_name == original
