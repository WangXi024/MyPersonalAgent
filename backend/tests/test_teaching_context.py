"""Use the actual pinned SDK with local synthetic models, never a provider request."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, UsageLimits
from pydantic_ai_harness.compaction import pin
from sqlalchemy import select

from app.agents.learning import AGENTS, RESEARCH_UI_AGENTS, TEACHING_AGENTS
from app.agents.schemas import TutorProposal
from app.agents.schemas_v3 import TutorProposalV3
from app.agents.teaching import TEACHING_PACK, TEACHING_PACK_SHA256, teaching_capabilities
from app.api import routes
from app.db.models import Message, Operation, User
from app.domain import commands, execution
from app.domain.conversation_history import search_history


async def test_official_deferred_capability_loads_only_fixed_instructions():
    calls = []
    identifier, description, body = TEACHING_PACK[0]
    def model(messages, info):
        calls.append(1)
        assert "read_tool_result" not in {tool.name for tool in info.function_tools}
        names = {tool.name for tool in info.function_tools}
        assert not names & {"read_file", "write_file", "run_command", "shell"}
        if len(calls) == 1:
            assert "load_capability" in names and description in info.instructions and body not in info.instructions
            return ModelResponse(parts=[ToolCallPart("load_capability", {"id": identifier}, "load-fixed-skill")])
        assert "teaching-retrieval-v1" in str(messages)
        assert "先让用户回忆或预测" in str(messages) + info.instructions
        return ModelResponse(parts=[TextPart("先预测返回值，再解释。")])
    agent = Agent(FunctionModel(model), capabilities=teaching_capabilities("plan"))
    result = await agent.run("教我函数", usage_limits=UsageLimits(request_limit=6, total_tokens_limit=80000))
    assert result.output == "先预测返回值，再解释。" and result.usage.requests == 2
    assert len(TEACHING_PACK_SHA256) == 64


async def test_actual_official_sliding_window_keeps_latest_and_tool_pairs():
    history = []
    for index in range(24):
        history.extend([ModelRequest(parts=[UserPromptPart(f"old-user-{index} " + "u" * 2000)]),
                        ModelResponse(parts=[TextPart(f"old-reply-{index} " + "r" * 2000)])])
        if index == 20:
            history.extend([ModelResponse(parts=[ToolCallPart("prior_tool", {}, "paired")]),
                            ModelRequest(parts=[ToolReturnPart("prior_tool", "saved result", "paired")])])
    def model(messages, info):
        assert len(messages) < len(history)
        assert "old-user-0 " not in str(messages)
        assert any(isinstance(part, UserPromptPart) and part.content == "current request" for message in messages for part in message.parts)
        calls = {part.tool_call_id for message in messages for part in message.parts if isinstance(part, ToolCallPart)}
        returns = {part.tool_call_id for message in messages for part in message.parts if isinstance(part, ToolReturnPart)}
        assert calls == returns and "paired" in calls
        return ModelResponse(parts=[TextPart("ok")])
    agent = Agent(FunctionModel(model), capabilities=[teaching_capabilities("plan")[3]])
    assert (await agent.run("current request", message_history=history)).output == "ok"
    assert len(history) == 50  # The caller's persisted originals were not trimmed.


async def test_official_token_floor_can_keep_many_small_messages():
    history = []
    for index in range(24):
        history.extend([ModelRequest(parts=[UserPromptPart(f"short-{index}")]), ModelResponse(parts=[TextPart("short reply")])])
    def model(messages, info):
        assert "short-0" in str(messages) and len(messages) >= 48
        return ModelResponse(parts=[TextPart("ok")])
    assert (await Agent(FunctionModel(model), capabilities=[teaching_capabilities("plan")[3]])
            .run("current", message_history=history)).output == "ok"


async def test_actual_official_output_truncation_needs_no_workspace_or_summary_model():
    calls = []
    def model(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("fetch_learning_resource", {}, "large-tool")])
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        assert len(returns[-1].content) <= 12000 and "TAIL" in returns[-1].content
        return ModelResponse(parts=[TextPart("bounded")])
    agent = Agent(FunctionModel(model), capabilities=[teaching_capabilities("plan")[4]])
    @agent.tool_plain
    async def fetch_learning_resource():
        return "HEAD" + "x" * 30000 + "TAIL"
    result = await agent.run("read")
    assert result.output == "bounded" and result.usage.requests == 2


def test_v1_v2_output_schemas_and_names_remain_registered_separately():
    for kind in AGENTS:
        assert AGENTS[kind].name == f"learning_{kind}_v1"
        assert RESEARCH_UI_AGENTS[kind].name == f"learning_{kind}_v2"
        assert TEACHING_AGENTS[kind].name == f"learning_{kind}_v3"
    block = {"type": "progress", "title": "真实进度"}
    with pytest.raises(ValidationError):
        TutorProposal.model_validate({"summary": "进度", "message": "进度", "ui_components": [block]})
    assert TutorProposalV3.model_validate({"summary": "进度", "message": "进度", "ui_components": [block]}).ui_components[0].type == "progress"


async def test_pinned_current_business_request_survives_real_compaction_after_large_tools():
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    calls = []
    def model(messages, info):
        calls.append(1)
        assert b"CURRENT-BUSINESS-SNAPSHOT" in ModelMessagesTypeAdapter.dump_json(messages)
        # Provider usage reaches the compaction trigger but stays within 6/80k.
        if len(calls) <= 3:
            return ModelResponse(parts=[ToolCallPart("fetch_learning_resource", {}, f"large-{len(calls)}")],
                                 usage=RequestUsage(input_tokens=7000 + len(calls) * 3000, output_tokens=100))
        ids = {part.tool_call_id for message in messages for part in message.parts if isinstance(part, ToolCallPart)}
        returns = {part.tool_call_id for message in messages for part in message.parts if isinstance(part, ToolReturnPart)}
        assert ids == returns
        return ModelResponse(parts=[TextPart("done")], usage=RequestUsage(input_tokens=16000, output_tokens=100))
    capabilities = teaching_capabilities("plan")
    agent = Agent(FunctionModel(model), capabilities=capabilities)
    @agent.tool_plain
    async def fetch_learning_resource():
        return "x" * 12000
    history = [ModelRequest(parts=[UserPromptPart("old unrelated first turn")]), ModelResponse(parts=[TextPart("old reply")])]
    result = await agent.run(pin("CURRENT-BUSINESS-SNAPSHOT " + "c" * 40000).content, message_history=history,
                             usage_limits=UsageLimits(request_limit=6, total_tokens_limit=80000))
    assert result.output == "done" and result.usage.requests == 4


async def seed_turn(user, conversation_id, text):
    accepted = await commands.send_message(user.id, conversation_id, {"content": text}, str(uuid4()))
    await execution.commit_result(accepted["operation_id"], {"kind": "tutor", "message": f"回复：{text}"})
    return accepted["operation_id"]


async def test_history_uses_official_search_chinese_fallback_and_strict_owner_cutoff(database):
    factory, user, other = database
    conversation = await routes.assistant_conversation(user)
    foreign = await routes.assistant_conversation(other)
    past = await seed_turn(user, conversation["id"], "上次讨论了生成器的惰性求值，标记past-public。")
    await seed_turn(other, foreign["id"], "生成器 foreign-user-secret")
    current = await commands.send_message(user.id, conversation["id"], {"content": "回忆生成器"}, str(uuid4()))
    await seed_turn(user, conversation["id"], "生成器 future-turn-secret")
    async with factory() as db, db.begin():
        op = await db.get(Operation, past)
        op.agent_history = [{"parts": [{"content": "raw-system-prompt-secret"}]}]
        op.result = {"private_quizzes": {"answer": "private-answer-secret"}}
    ctx = SimpleNamespace(deps={"operation": {"id": current["operation_id"]}}, conversation_id=conversation["id"], tool_call_id="history-1")
    result = await search_history(ctx, "生成器")
    assert "past-public" in result and "中文原文匹配" in result
    assert all(secret not in result for secret in ["foreign-user-secret", "future-turn-secret", "raw-system-prompt-secret", "private-answer-secret"])
    ctx.tool_call_id = "history-2"
    assert "past-public" not in await search_history(ctx, "生成器", run_id="invented-run")
    ctx.conversation_id = foreign["id"]
    assert "past-public" not in await search_history(ctx, "生成器")
    async with factory() as db:
        assert len(list(await db.scalars(select(Message).where(Message.conversation_id == conversation["id"])))) == 5


async def test_history_rejects_revoked_scope_and_caps_calls(database):
    factory, user, _ = database
    conversation = await routes.assistant_conversation(user)
    await seed_turn(user, conversation["id"], "literal needle")
    current = await commands.send_message(user.id, conversation["id"], {"content": "recall"}, str(uuid4()))
    ctx = SimpleNamespace(deps={"operation": {"id": current["operation_id"]}}, conversation_id=conversation["id"], tool_call_id="h0")
    for index in range(8):
        ctx.tool_call_id = f"h{index}"
        assert "needle" in await search_history(ctx, "needle")
    ctx.tool_call_id = "h8"
    assert "上限" in await search_history(ctx, "needle")
    async with factory() as db, db.begin():
        (await db.get(User, user.id)).memory_control_revision += 1
    ctx.tool_call_id = "h0"
    assert "授权已变化" in await search_history(ctx, "needle")


async def test_history_cannot_cross_another_goal_owned_by_same_user(database):
    from app.db.models import Conversation
    from test_domain import active_goal
    factory, user, _ = database
    async with factory() as db, db.begin():
        (await db.get(User, user.id)).daily_minutes = 120
    first, _, _ = await active_goal(user)
    second, _, _ = await active_goal(user)
    async with factory() as db:
        conversations = {item.goal_id: item.id for item in await db.scalars(select(Conversation).where(Conversation.user_id == user.id))}
    await seed_turn(user, conversations[first["id"]], "闭包 current-goal-public")
    await seed_turn(user, conversations[second["id"]], "闭包 other-goal-secret")
    command = await commands.send_message(user.id, conversations[first["id"]], {"content": "回忆"}, str(uuid4()))
    ctx = SimpleNamespace(deps={"operation": {"id": command["operation_id"]}}, conversation_id=conversations[first["id"]], tool_call_id="goal-history")
    result = await search_history(ctx, "闭包")
    assert "current-goal-public" in result and "other-goal-secret" not in result
