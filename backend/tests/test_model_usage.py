import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.api import routes
from app.db.models import ModelConfig, Operation
from app.domain import commands, execution
from app.domain.common import public_operation
from app.domain.model_usage import public_usage, summarize_usage
from app.integrations import model_catalog
from app.integrations.settings import RuntimeSettings
from app.main import app


@pytest.fixture
def priced_catalog(tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    models = [{"id": "learning-agent", "name": "测试模型", "provider": "bailian",
               "input_cny_per_million": "12", "output_cny_per_million": "36",
               "api_key": "test-secret-must-not-appear"}]
    path.write_text(json.dumps(models))
    monkeypatch.setattr(model_catalog, "runtime_settings", lambda: RuntimeSettings(chat_model_catalog_path=path))
    return path, models


async def chat(user, content="测试用量"):
    conversation = await routes.assistant_conversation(user)
    command = await commands.send_message(user.id, conversation["id"], {"content": content}, str(uuid4()))
    return conversation, command["operation_id"]


async def test_usage_snapshots_prices_and_counts_attempts_once(database, priced_catalog):
    factory, user, _ = database
    conversation, oid = await chat(user)
    path, models = priced_catalog
    path.write_text(json.dumps([{**models[0], "name": "改名模型", "input_cny_per_million": "999"}]))
    await execution.reserve_model_request(oid)
    await execution.record_model_tokens(oid, 1000, 200, 1)
    await execution.record_model_tokens(oid, 1000, 200, 1)  # repeated delivery does not bill twice
    await execution.reserve_model_request(oid)
    await execution.record_model_tokens(oid, 500, 100, 2)
    await execution.record_model_usage(oid, {"requests": 2, "input_tokens": 1500, "output_tokens": 300})
    await execution.commit_result(oid, {"kind": "tutor", "message": "测试回复"})
    history = await routes.messages(conversation["id"], 0, 100, user)
    assert len(history["items"]) == 2
    usage = history["items"][1]["usage"]
    assert history["items"][0]["usage"] is None
    assert usage["model_name"] == "测试模型"
    assert usage["input_tokens"] == 1500 and usage["output_tokens"] == 300
    assert usage["requests"] == 2 and usage["total_tokens"] == 1800
    assert usage["estimated_cost_cny"] == "0.028800" and usage["cost_complete"]
    total = await routes.conversation_usage(conversation["id"], user)
    assert total["requests"] == 2 and total["total_tokens"] == 1800
    assert total["estimated_cost_cny"] == "0.028800"
    async with factory() as db:
        public = public_operation(await db.get(Operation, oid))
        assert "usage_details" not in public
        assert "test-secret" not in json.dumps(public)


async def test_timeouts_and_manual_retries_preserve_partial_usage(database, priced_catalog):
    factory, user, _ = database
    conversation, oid = await chat(user)
    await execution.reserve_model_request(oid)  # timed out with no usage
    await execution.reserve_model_request(oid)
    await execution.record_model_tokens(oid, 100, 50, 2)
    await execution.mark_failed(oid, "invalid_chat_action", "unused")
    retry = await routes.retry_operation(oid, user, str(uuid4()))
    await execution.reserve_model_request(retry["operation_id"])
    await execution.record_model_tokens(retry["operation_id"], 200, 100, 1)
    await execution.commit_result(retry["operation_id"], {"kind": "tutor", "message": "重试回复"})
    total = await routes.conversation_usage(conversation["id"], user)
    assert total["requests"] == 3 and total["total_tokens"] == 450
    assert total["estimated_cost_cny"] == "0.009000"
    assert not total["usage_complete"] and not total["cost_complete"]
    async with factory() as db:
        assert public_usage(await db.get(Operation, retry["operation_id"]))["cost_complete"]


async def test_missing_response_usage_and_legacy_prices_are_unknown(database, priced_catalog):
    factory, user, _ = database
    _, oid = await chat(user)
    await execution.reserve_model_request(oid)
    await execution.record_model_tokens(oid, 0, 0, 1)
    async with factory() as db, db.begin():
        op = await db.get(Operation, oid)
        assert public_usage(op)["estimated_cost_cny"] is None
        assert not public_usage(op)["usage_complete"]
        op.usage_details = None  # historical rows do not gain today's prices
        op.input_tokens, op.output_tokens = 1000, 500
        usage = public_usage(op)
        assert usage["total_tokens"] == 1500 and usage["estimated_cost_cny"] is None
        assert not usage["pricing_known"] and not usage["usage_complete"]
        empty = Operation(model_requests=0, input_tokens=0, output_tokens=0, versions={})
        assert summarize_usage([op, empty])["estimated_cost_cny"] is None


async def test_managed_model_pricing_is_frozen_for_each_operation(database, priced_catalog):
    factory, user, _ = database
    async with factory() as db, db.begin():
        row = ModelConfig(name="自配模型", provider="zhipu", model_id="test-glm", api_base="https://local-test.invalid",
            input_cny_per_million="2", output_cny_per_million="8", usd_cny_rate="7", gateway_alias="configured-test-v1",
            credential_name="private-credential-reference", aliases=["configured-test-v1"], enabled=True,
            version=1, last_request_key="test-key", last_request_hash="test-hash")
        db.add(row)
        await db.flush()
        mid = row.id
    conversation = await routes.assistant_conversation(user)
    await routes.select_conversation_model(conversation["id"],
        routes.ConversationModelInput(model_id=f"model-{mid}", expected_version=1), user)
    _, oid = await chat(user)
    async with factory() as db, db.begin():
        row = await db.get(ModelConfig, mid)
        row.input_cny_per_million, row.output_cny_per_million = "20", "80"
        row.gateway_alias, row.aliases = "configured-test-v2", ["configured-test-v1", "configured-test-v2"]
    await execution.reserve_model_request(oid)
    await execution.record_model_tokens(oid, 1000, 500, 1)
    async with factory() as db:
        usage = public_usage(await db.get(Operation, oid))
        assert usage["model_id"] == "test-glm" and usage["estimated_cost_cny"] == "0.006000"
        assert "private-credential-reference" not in json.dumps(usage)


async def test_conversation_usage_is_private_and_not_limited_to_recent_messages(database, priced_catalog):
    factory, user, other = database
    conversation, oid = await chat(user)
    foreign, _ = await chat(other)
    async with factory() as db, db.begin():
        original = await db.get(Operation, oid)
        for i in range(105):
            db.add(Operation(user_id=user.id, kind="tutor", payload={"conversation_id": conversation["id"]},
                versions=original.versions, usage_details={**original.usage_details, "reported_requests": [1]},
                model_requests=1, input_tokens=10, output_tokens=5, idempotency_key=f"test-usage-{i}", request_hash="test"))
        db.add(Operation(user_id=user.id, kind="plan", payload={"conversation_id": conversation["id"]},
            model_requests=99, input_tokens=999, output_tokens=999, idempotency_key="excluded-plan", request_hash="test"))
    path = f"/api/v1/conversations/{conversation['id']}/usage"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        assert (await client.get(path)).status_code == 401
        await client.post("/api/v1/auth/login", json={"username": "learner", "password": "test-only-long-password"},
                          headers={"Origin": "http://localhost:8080"})
        assert (await client.get(f"/api/v1/conversations/{foreign['id']}/usage")).status_code == 404
        total = (await client.get(path)).json()
        assert total["requests"] == 105 and total["total_tokens"] == 1575
        assert total["estimated_cost_cny"] == "0.031500" and total["cost_complete"]
    async with factory() as db:
        assert len(list(await db.scalars(select(Operation)))) == 108


async def test_usage_migration_preserves_existing_token_records(database):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    factory, user, _ = database
    _, oid = await chat(user)
    async with factory() as db, db.begin():
        op = await db.get(Operation, oid)
        op.model_requests, op.input_tokens, op.output_tokens = 2, 1000, 200
        await db.flush()
        # Only this test's isolated schema is changed to the pre-migration shape.
        await db.execute(text("ALTER TABLE operations DROP COLUMN usage_details"))
        connection = await db.connection()

        def upgrade(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                import_module("migrations.versions.0005_model_usage").upgrade()

        await connection.run_sync(upgrade)
        row = (await db.execute(text("SELECT model_requests, input_tokens, output_tokens, usage_details FROM operations WHERE id = :id"),
                                {"id": oid})).one()
        assert tuple(row) == (2, 1000, 200, None)
