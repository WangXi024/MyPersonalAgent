import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api import model_configs, routes
from app.api.auth import current_user
from app.db.models import ModelConfig, Operation
from app.domain import commands, execution
from app.domain.common import DomainError
from app.integrations.model_catalog import available_models
from app.integrations.model_management import ModelGateway
from app.main import app

PAYLOAD = {"name": "测试模型", "provider": "bailian", "model_id": "test-model",
           "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
           "api_key": "test-only-non-secret-key", "input_cny_per_million": "12",
           "output_cny_per_million": "36", "usd_cny_rate": "7", "expected_version": 0}


@pytest.fixture
def gateway(monkeypatch):
    class FakeGateway:
        prepared = []
        probes = []
        fail = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prepare(self, *args, **kwargs):
            if self.fail:
                raise DomainError("model_gateway_error", "模型服务暂不可用", 502)
            self.prepared.append((args, kwargs))

        async def probe(self, alias):
            self.probes.append(alias)
            return {"input_tokens": 100, "output_tokens": 10}

    monkeypatch.setattr(model_configs, "ModelGateway", FakeGateway)
    monkeypatch.setattr(model_configs, "get_settings", lambda: SimpleNamespace(model_admin_username="learner"))
    return FakeGateway


async def test_admin_and_csrf_guards_and_secret_not_returned(database, gateway):
    _, user, other = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        assert (await client.get("/api/v1/model-configs")).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"username": "learner", "password": "test-only-long-password"},
                                  headers={"Origin": "http://localhost:8080"})
        headers = {"Origin": "http://localhost:8080", "X-CSRF-Token": login.json()["csrf_token"],
                   "Idempotency-Key": "create-model-test"}
        assert (await client.post("/api/v1/model-configs", json=PAYLOAD)).status_code == 403
        response = await client.post("/api/v1/model-configs", json=PAYLOAD, headers=headers)
        assert response.status_code == 201
        assert PAYLOAD["api_key"] not in response.text
        assert not any(field in response.json() for field in ("api_key", "credential_name", "aliases", "gateway_alias", "last_request_hash"))
        assert response.json()["enabled"] is False and response.json()["key_configured"] is True
        replay = await client.post("/api/v1/model-configs", json=PAYLOAD, headers=headers)
        assert replay.json() == response.json()
        assert len(gateway.prepared) == 1
        conflict = await client.post("/api/v1/model-configs", json={**PAYLOAD, "api_key": "different-test-key"}, headers=headers)
        assert conflict.status_code == 409
        app.dependency_overrides[current_user] = lambda: other
        try:
            assert (await client.get("/api/v1/model-configs")).status_code == 403
            assert (await client.post("/api/v1/model-configs", json=PAYLOAD, headers=headers)).status_code == 403
        finally:
            app.dependency_overrides.clear()


@pytest.mark.parametrize("provider,url", [
    ("bailian", "http://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("bailian", "https://127.0.0.1/compatible-mode/v1"),
    ("bailian", "https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1"),
    ("bailian", "https://dashscope.aliyuncs.com/compatible-mode/v1?key=test"),
    ("bailian", "https://user:pass@dashscope.aliyuncs.com/compatible-mode/v1"),
    ("zhipu", "https://open.bigmodel.cn/api/coding/paas/v4"),
    ("ark", "https://ark.cn-beijing.volces.com:8080/api/v3"),
    ("zhipu", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
])
def test_invalid_provider_endpoints_are_rejected(provider, url):
    with pytest.raises(ValidationError):
        model_configs.ModelInput.model_validate({**PAYLOAD, "provider": provider, "api_base": url})


async def test_edit_reuses_credentials_and_preserves_submitted_model_versions(database, gateway):
    factory, user, _ = database
    created = await model_configs.create_model(model_configs.ModelInput(**PAYLOAD), user, "create-version-test")
    with pytest.raises(DomainError, match="请先通过连接测试"):
        await model_configs.set_enabled(created["id"], model_configs.StateInput(expected_version=1, enabled=True), user)
    tested = await model_configs.test_model(created["id"], model_configs.TestInput(expected_version=1), user, "test-once-key")
    assert tested["test_result"]["estimated_cost_cny"] == "0.001560"
    await model_configs.test_model(created["id"], model_configs.TestInput(expected_version=1), user, "test-once-key")
    assert len(gateway.probes) == 1
    enabled = await model_configs.set_enabled(created["id"], model_configs.StateInput(expected_version=1, enabled=True), user)
    conversation = await routes.assistant_conversation(user)
    await routes.select_conversation_model(conversation["id"], routes.ConversationModelInput(
        model_id="model-" + created["id"], expected_version=1), user)
    first = await commands.send_message(user.id, conversation["id"], {"content": "first"}, "model-snapshot-one")
    old_alias = (await execution.reserve_model_request(first["operation_id"]))["model_alias"]
    payload = {**PAYLOAD, "model_id": "test-updated", "api_key": None, "expected_version": enabled["version"]}
    edited = await model_configs.update_model(created["id"], model_configs.ModelInput(**payload), user, "edit-version-test")
    assert not edited["enabled"] and edited["test_result"] is None
    assert gateway.prepared[-1][0][4] is None
    assert gateway.prepared[0][0][3] == gateway.prepared[1][0][3]
    assert (await execution.reserve_model_request(first["operation_id"]))["model_alias"] == old_alias
    with pytest.raises(DomainError, match="所选模型已停用"):
        await commands.send_message(user.id, conversation["id"], {"content": "new"}, "new-when-disabled")
    await model_configs.test_model(created["id"], model_configs.TestInput(expected_version=edited["version"]), user, "test-new-version")
    enabled = await model_configs.set_enabled(created["id"], model_configs.StateInput(expected_version=edited["version"], enabled=True), user)
    second = await commands.send_message(user.id, conversation["id"], {"content": "new"}, "new-when-enabled")
    new_alias = (await execution.reserve_model_request(second["operation_id"]))["model_alias"]
    assert new_alias != old_alias
    await model_configs.set_enabled(created["id"], model_configs.StateInput(expected_version=enabled["version"], enabled=False), user)
    assert (await execution.reserve_model_request(second["operation_id"]))["model_alias"] == new_alias
    async with factory() as db:
        assert len((await db.get(ModelConfig, created["id"])).aliases) == 2
        assert (await db.get(Operation, first["operation_id"])).versions["model_alias"] == old_alias
        assert all(item["id"] != "model-" + created["id"] for item in await available_models(db))


async def test_preset_copy_and_gateway_failure_cannot_publish_partial_configuration(database, gateway, monkeypatch):
    factory, user, _ = database
    monkeypatch.setattr(model_configs, "presets", lambda: [{
        "id": "learning-agent", "provider": "bailian", "api_base": PAYLOAD["api_base"],
    }])
    payload = model_configs.ModelInput(**{**PAYLOAD, "api_key": None, "source_model_id": "learning-agent"})
    gateway.fail = True
    with pytest.raises(DomainError):
        await model_configs.create_model(payload, user, "copy-preset-test")
    async with factory() as db:
        assert list(await db.scalars(select(ModelConfig))) == []
    gateway.fail = False
    result = await model_configs.create_model(payload, user, "copy-preset-test")
    assert result["version"] == 1 and gateway.prepared[-1][1]["source_alias"] == "learning-agent"
    with pytest.raises(DomainError, match="需要填写密钥"):
        await model_configs.update_model(result["id"], model_configs.ModelInput(**{
            **PAYLOAD, "api_key": None, "expected_version": 1, "provider": "zhipu",
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
        }), user, "change-provider-test")


async def test_gateway_uses_named_credentials_prices_and_restricted_inference_key():
    requests = []
    models = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/model/info":
            return httpx.Response(200, json={"data": models})
        if request.url.path == "/model/new":
            models.append(json.loads(request.content))
            return httpx.Response(200, json={"model_id": "version-one"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "configured-version-one"}]})
        if request.url.path == "/v1/chat/completions":
            assert request.headers["Authorization"] == "Bearer test-app-key"
            return httpx.Response(401, json={"error": "test-secret-must-not-be-returned"})
        return httpx.Response(200, json={"success": True})

    gateway = ModelGateway()
    gateway.admin_key, gateway.app_key = "test-admin-key", "test-app-key"
    async with httpx.AsyncClient(base_url="https://gateway.test", transport=httpx.MockTransport(handler)) as client:
        gateway.client = client
        values = {k: v for k, v in PAYLOAD.items() if k not in {"api_key", "expected_version"}}
        await gateway.prepare("version-one", "configured-version-one", values, "named-credential", PAYLOAD["api_key"])
        await gateway.prepare("version-one", "configured-version-one", values, "named-credential")
        assert len(models) == 1
        model = models[0]
        assert model["litellm_params"]["litellm_credential_name"] == "named-credential"
        assert "api_key" not in model["litellm_params"]
        assert model["model_info"]["access_groups"] == ["learning-configured"]
        assert model["model_info"]["input_cost_per_token"] == pytest.approx(12 / 7 / 1000000)
        with pytest.raises(DomainError) as caught:
            await gateway.probe("configured-version-one")
        assert "test-secret" not in str(caught.value)
        assert caught.value.__context__ is None
        body = json.loads(requests[-1].content)
        assert body["max_tokens"] == 512 and len(body["messages"]) == 1
