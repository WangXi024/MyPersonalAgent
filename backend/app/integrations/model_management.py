"""Restricted gateway administration; never return provider credentials or raw errors."""
import json
from decimal import Decimal

import httpx

from app.domain.common import DomainError
from app.integrations.settings import runtime_settings


class ModelGateway:
    async def __aenter__(self):
        settings = runtime_settings()
        try:
            self.admin_key = settings.litellm_admin_key_path.read_text().strip()
        except (AttributeError, OSError):
            raise DomainError("model_management_unavailable", "模型管理尚未配置，请检查部署配置", 503) from None
        self.app_key = settings.litellm_api_key.get_secret_value()
        self.client = httpx.AsyncClient(
            base_url=settings.litellm_base_url.removesuffix("/v1").rstrip("/"),
            timeout=30, trust_env=False, follow_redirects=False,
        )
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, method, path, body=None, *, inference=False, conflict_ok=False):
        status = None
        try:
            response = await self.client.request(
                method, path, json=body,
                headers={"Authorization": "Bearer " + (self.app_key if inference else self.admin_key)},
            )
            status = response.status_code
            if conflict_ok and status == 409:
                return {}
            if response.is_success:
                return response.json()
        except (httpx.HTTPError, ValueError):
            pass
        # Error payloads and chained exceptions may contain credentials; discard them.
        messages = {
            401: "认证失败，请检查密钥是否有效",
            403: "没有访问权限，请检查模型权限或网关配置",
            404: "模型或接口不存在，请检查模型 ID 和 API 地址",
            429: "额度或速率受限，请稍后重试",
        }
        raise DomainError("model_gateway_error", messages.get(status, "模型服务暂不可用，请检查配置后重试"), 502)

    async def prepare(self, deployment_id, alias, values, credential_name, api_key=None, source_alias=None):
        if api_key is not None or source_alias:
            credential = {"credential_name": credential_name, "credential_info": {"custom_llm_provider": "openai"}}
            if api_key is not None:
                credential["credential_values"] = {"api_key": api_key}
            else:
                models = await self.request("GET", "/model/info")
                source = next((item for item in models.get("data", []) if item.get("model_name") == source_alias), None)
                if not source or not source.get("model_info", {}).get("id"):
                    raise DomainError("source_model_missing", "部署预置模型暂不可用，请刷新后重试", 503)
                credential["model_id"] = source["model_info"]["id"]
            await self.request("POST", "/credentials", credential, conflict_ok=True)
        models = await self.request("GET", "/model/info")
        if not any(item.get("model_info", {}).get("id") == deployment_id for item in models.get("data", [])):
            rate = Decimal(values["usd_cny_rate"])
            await self.request("POST", "/model/new", {
                "model_name": alias,
                "litellm_params": {
                    "model": "openai/" + values["model_id"], "api_base": values["api_base"],
                    "litellm_credential_name": credential_name, "timeout": 630, "max_retries": 0,
                },
                "model_info": {
                    "id": deployment_id, "mode": "chat", "access_groups": ["learning-configured"],
                    "input_cost_per_token": float(Decimal(values["input_cny_per_million"]) / rate / 1000000),
                    "output_cost_per_token": float(Decimal(values["output_cny_per_million"]) / rate / 1000000),
                },
            })
        visible = await self.request("GET", "/v1/models", inference=True)
        if not any(item.get("id") == alias for item in visible.get("data", [])):
            raise DomainError("model_not_ready", "模型尚未同步到应用，请稍后重试保存", 503)

    async def probe(self, alias):
        result = await self.request("POST", "/v1/chat/completions", {
            "model": alias, "max_tokens": 512,
            "messages": [{"role": "user", "content": "这是连接测试。请调用 connection_check 工具，message 必须为：连接正常。"}],
            "tools": [{"type": "function", "function": {
                "name": "connection_check", "description": "验证中文与结构化工具调用",
                "parameters": {"type": "object", "properties": {"message": {"type": "string", "enum": ["连接正常"]}},
                               "required": ["message"], "additionalProperties": False},
            }}],
            "tool_choice": {"type": "function", "function": {"name": "connection_check"}},
        }, inference=True)
        try:
            calls = result["choices"][0]["message"]["tool_calls"]
            assert len(calls) == 1
            assert calls[0]["function"]["name"] == "connection_check"
            assert json.loads(calls[0]["function"]["arguments"]) == {"message": "连接正常"}
        except (KeyError, IndexError, TypeError, ValueError, AssertionError):
            raise DomainError("model_capability_error", "已收到响应，但中文或结构化工具调用未通过验证", 422) from None
        usage = result.get("usage") or {}
        if isinstance(usage, dict) and all(type(usage.get(name)) is int and usage[name] >= 0
                                           for name in ("prompt_tokens", "completion_tokens")):
            return {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]}
        return {}
