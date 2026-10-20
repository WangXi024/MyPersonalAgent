import json
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select

from app.api.auth import current_user
from app.config import get_settings
from app.db.models import ModelConfig, now
from app.db.session import SessionFactory
from app.domain.common import DomainError, digest, emit, lock_user, serialize, version
from app.integrations.model_catalog import ChatModel
from app.integrations.model_management import ModelGateway
from app.integrations.settings import runtime_settings

router = APIRouter(prefix="/model-configs", tags=["model configuration"])


async def admin(user=Depends(current_user)):
    if not get_settings().model_admin_username or user.username != get_settings().model_admin_username:
        raise DomainError("forbidden", "仅部署管理员可以管理模型配置", 403)
    return user


def request_key(idempotency_key: str = Header(..., min_length=8, max_length=160)):
    return idempotency_key


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["bailian", "zhipu", "ark"]
    model_id: str = Field(min_length=1, max_length=200)
    api_base: str = Field(min_length=1, max_length=500)
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=4096)
    input_cny_per_million: Decimal = Field(ge=0, le=1000000, max_digits=18, decimal_places=6)
    output_cny_per_million: Decimal = Field(ge=0, le=1000000, max_digits=18, decimal_places=6)
    usd_cny_rate: Decimal = Field(default=Decimal("7"), gt=0, le=1000, max_digits=12, decimal_places=6)
    expected_version: int = Field(default=0, ge=0)
    source_model_id: str | None = Field(default=None, max_length=100)

    @field_validator("api_base")
    @classmethod
    def validate_base(cls, value, info):
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        provider = info.data.get("provider")
        allowed = {
            "bailian": (host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
                        or host.endswith(".maas.aliyuncs.com")) and parsed.path.rstrip("/") == "/compatible-mode/v1",
            "zhipu": host == "open.bigmodel.cn" and parsed.path.rstrip("/") == "/api/paas/v4",
            "ark": host.startswith("ark.") and host.endswith(".volces.com") and parsed.path.rstrip("/") == "/api/v3",
        }
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or not allowed.get(provider)):
            raise ValueError("请使用对应供应商的官方 HTTPS 标准推理地址")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value):
        if value is not None:
            raw = value.get_secret_value()
            if raw != raw.strip() or any(c in raw for c in "<>\r\n\x00") or raw.startswith("os.environ/"):
                raise ValueError("密钥不能包含占位符、环境引用或多余空白")
        return value


class StateInput(BaseModel):
    expected_version: int = Field(ge=1)
    enabled: bool


class TestInput(BaseModel):
    expected_version: int = Field(ge=1)


class PresetModel(ChatModel):
    api_base: str | None = None
    input_cny_per_million: str | None = None
    output_cny_per_million: str | None = None
    usd_cny_rate: str = "7"


def presets():
    path = runtime_settings().chat_model_catalog_path
    if path is None:
        return []
    try:
        return [PresetModel.model_validate(item).model_dump() for item in json.loads(path.read_text())]
    except (OSError, ValueError, TypeError):
        raise DomainError("model_catalog_unavailable", "部署模型列表暂不可用", 503) from None


def public_model(row):
    return {**serialize(row, {"gateway_alias", "credential_name", "aliases", "last_request_key",
                             "last_request_hash", "last_test_key"}), "key_configured": True}


async def model_row(db, model_id):
    row = await db.scalar(select(ModelConfig).where(ModelConfig.id == str(model_id)).with_for_update())
    if row is None:
        raise DomainError("not_found", "模型配置不存在", 404)
    return row


@router.get("")
async def list_models(user=Depends(admin)):
    async with SessionFactory() as db:
        rows = await db.scalars(select(ModelConfig).order_by(ModelConfig.created_at))
        return {"items": [public_model(row) for row in rows], "presets": presets()}


async def save_model(model_id, payload, user, key, *, creating=False):
    values = payload.model_dump(mode="json", exclude={"api_key", "expected_version", "source_model_id"})
    secret = payload.api_key.get_secret_value() if payload.api_key else None
    fingerprint = digest({**values, "api_key": secret, "source_model_id": payload.source_model_id,
                          "expected_version": payload.expected_version})
    deployment_id = str(uuid5(NAMESPACE_URL, f"{model_id}:{key}:{fingerprint}"))
    alias = "configured-" + deployment_id
    async with SessionFactory() as db, db.begin():
        _, stream = await lock_user(db, user.id)
        row = await db.scalar(select(ModelConfig).where(ModelConfig.id == model_id).with_for_update())
        if row and row.last_request_key == key:
            if row.last_request_hash != fingerprint:
                raise DomainError("idempotency_conflict", "该请求标识已用于不同配置")
            return public_model(row)
        if creating and row:
            raise DomainError("idempotency_conflict", "该配置已存在，请刷新后编辑")
        if not creating and row is None:
            raise DomainError("not_found", "模型配置不存在", 404)
        version(row.version if row else 0, payload.expected_version)
        source = next((item for item in presets() if item["id"] == payload.source_model_id), None)
        if secret is None:
            previous = {"provider": row.provider, "api_base": row.api_base} if row else source
            if not previous or previous["provider"] != payload.provider or previous["api_base"] != payload.api_base:
                raise DomainError("api_key_required", "新增模型或更换供应商、API 地址时需要填写密钥", 422)
        credential = "learning-credential-" + deployment_id if secret is not None or row is None else row.credential_name
        async with ModelGateway() as gateway:
            await gateway.prepare(deployment_id, alias, values, credential, secret,
                                  source_alias=source["id"] if row is None and source and secret is None else None)
        if row is None:
            row = ModelConfig(id=model_id, **values, gateway_alias=alias, credential_name=credential,
                              aliases=[alias], enabled=False, version=1, last_request_key=key,
                              last_request_hash=fingerprint)
            db.add(row)
        else:
            for name, value in values.items():
                setattr(row, name, value)
            row.gateway_alias, row.credential_name = alias, credential
            row.aliases = [*row.aliases, alias]
            row.enabled = False
            row.version += 1
            row.test_result, row.last_test_key = None, None
            row.last_request_key, row.last_request_hash = key, fingerprint
        await db.flush()
        await emit(db, stream, "model.configured", row.id, "模型配置已保存，等待连接测试", entity_version=row.version)
        return public_model(row)


@router.post("", status_code=201)
async def create_model(payload: ModelInput, user=Depends(admin), key=Depends(request_key)):
    model_id = str(uuid5(NAMESPACE_URL, f"learning-model:{user.id}:{key}"))
    return await save_model(model_id, payload, user, key, creating=True)


@router.put("/{model_id}")
async def update_model(model_id: UUID, payload: ModelInput, user=Depends(admin), key=Depends(request_key)):
    return await save_model(str(model_id), payload, user, key)


@router.post("/{model_id}/enabled")
async def set_enabled(model_id: UUID, payload: StateInput, user=Depends(admin)):
    async with SessionFactory() as db, db.begin():
        _, stream = await lock_user(db, user.id)
        row = await model_row(db, model_id)
        if row.enabled != payload.enabled:
            version(row.version, payload.expected_version)
            if payload.enabled and (row.test_result or {}).get("status") != "passed":
                raise DomainError("model_not_verified", "请先通过连接测试，再启用模型")
            row.enabled = payload.enabled
            row.version += 1
            await emit(db, stream, "model.configured", row.id, "模型已启用" if row.enabled else "模型已停用",
                       entity_version=row.version)
        return public_model(row)


@router.post("/{model_id}/test")
async def test_model(model_id: UUID, payload: TestInput, user=Depends(admin), key=Depends(request_key)):
    async with SessionFactory() as db, db.begin():
        _, stream = await lock_user(db, user.id)
        row = await model_row(db, model_id)
        if row.last_test_key == key and row.test_result:
            return public_model(row)
        version(row.version, payload.expected_version)
        try:
            async with ModelGateway() as gateway:
                usage = await gateway.probe(row.gateway_alias)
            result = {"status": "passed", "message": "中文和结构化工具调用已通过", **usage}
            if usage:
                cost = (Decimal(row.input_cny_per_million) * usage["input_tokens"]
                        + Decimal(row.output_cny_per_million) * usage["output_tokens"]) / 1000000
                result["estimated_cost_cny"] = str(cost.quantize(Decimal("0.000001")))
        except DomainError as error:
            result = {"status": "failed", "message": error.message}
        row.test_result = {**result, "at": now().isoformat()}
        row.last_test_key = key
        await emit(db, stream, "model.configured", row.id, "模型连接测试已完成", entity_version=row.version)
        return public_model(row)
