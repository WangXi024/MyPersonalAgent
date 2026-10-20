"""Explicit, bounded live provider capability check. Never runs at startup."""
import argparse
import asyncio
import re
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from app.integrations.settings import runtime_settings


class ProbeResult(BaseModel):
    total: Literal[42]
    explanation: str


async def probe():
    settings = runtime_settings()
    key = settings.litellm_api_key.get_secret_value()
    if not key:
        raise ValueError("模型网关密钥未配置")
    calls = []
    client = AsyncOpenAI(base_url=settings.litellm_base_url, api_key=key, max_retries=0)
    agent = Agent(OpenAIChatModel(settings.litellm_model_alias,
                  provider=OpenAIProvider(openai_client=client)), output_type=ProbeResult, retries=1,
                  instructions="先调用 add 工具计算，再用简体中文解释结果，最后输出规定结构。",
                  model_settings={"max_tokens": 512, "timeout": 60})

    @agent.tool_plain
    def add(a: int, b: int) -> int:
        """Add the two integers and return their sum."""
        calls.append((a, b))
        return a + b

    print("开始真实模型能力探测：最多 4 次请求，每次最多 512 输出 Token；可能产生供应商费用。")
    try:
        result = await agent.run("请用工具计算 19 加 23，并解释结果。",
                                 usage_limits=UsageLimits(request_limit=4, total_tokens_limit=6000))
        if (19, 23) not in calls or not re.search(r"[\u4e00-\u9fff]", result.output.explanation):
            raise ValueError("模型未满足工具调用与中文输出要求")
        print(f"PASS: 工具调用、工具结果续接、结构化校验、中文输出；requests={result.usage.requests}, "
              f"input_tokens={result.usage.input_tokens}, output_tokens={result.usage.output_tokens}")
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model", required=True, action="store_true")
    parser.parse_args()
    try:
        asyncio.run(probe())
    except Exception as error:  # noqa: BLE001 -- never print provider error payloads or credentials
        print(f"FAIL: {type(error).__name__}；检查模型 ID、密钥、端点及工具调用支持。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
