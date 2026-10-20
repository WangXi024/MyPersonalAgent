import json
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import select

from app.db.models import ModelConfig

from app.domain.common import DomainError
from app.integrations.settings import runtime_settings


class ChatModel(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=200)
    provider: Literal["bailian", "zhipu", "ark", "gateway"]


def configured_models() -> list[dict]:
    settings = runtime_settings()
    if settings.chat_model_catalog_path is None:
        return [{"id": settings.litellm_model_alias, "name": "默认模型", "provider": "gateway"}]
    try:
        models = TypeAdapter(list[ChatModel]).validate_python(
            json.loads(settings.chat_model_catalog_path.read_text())
        )
        if not models or len({model.id for model in models}) != len(models):
            raise ValueError()
    except (OSError, ValueError, ValidationError):
        raise DomainError("model_catalog_unavailable", "模型列表暂不可用，请稍后重试", 503) from None
    return [model.model_dump() for model in models]


def resolve_model(alias: str | None) -> str:
    alias = alias or runtime_settings().litellm_model_alias
    if alias not in {model["id"] for model in configured_models()}:
        raise DomainError("model_unavailable", "所选模型已不可用，请重新选择对话模型")
    return alias


async def available_models(db) -> list[dict]:
    managed = await db.scalars(select(ModelConfig).where(ModelConfig.enabled.is_(True)).order_by(ModelConfig.created_at))
    return configured_models() + [
        {"id": f"model-{row.id}", "name": row.name, "provider": row.provider} for row in managed
    ]


async def selected_model(db, alias: str | None, *, snapshot=False) -> str:
    if alias and alias.startswith("model-"):
        row = await db.scalar(select(ModelConfig).where(ModelConfig.id == alias[6:]).with_for_update())
        if row is None or not row.enabled:
            raise DomainError("model_unavailable", "所选模型已停用或不存在，请重新选择对话模型")
        return row.gateway_alias if snapshot else alias
    return resolve_model(alias)


async def request_model(db, alias: str | None) -> str:
    if alias and alias.startswith("configured-"):
        # Submitted operations retain their immutable deployment after edits or disablement.
        rows = await db.scalars(select(ModelConfig))
        if any(alias in row.aliases for row in rows):
            return alias
        raise DomainError("model_unavailable", "该请求使用的模型版本已不可用")
    return resolve_model(alias)
