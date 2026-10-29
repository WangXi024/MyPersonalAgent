from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.schemas import TutorProposal
from app.agents.ui import build_ui_components
from app.api import routes
from app.db.models import Operation
from app.domain import commands, execution
from app.domain.common import DomainError


SOURCE = {
    "id": "source-python", "title": "Python 教程", "url": "https://docs.python.org/3/tutorial/",
    "snippet": "官方教程", "retrieved_at": "2026-10-29T01:00:00+00:00",
}


def test_display_proposals_cannot_supply_code_commands_or_source_urls():
    reply = {"summary": "资料", "message": "参考下方资料。"}
    for block in [
        {"type": "html", "html": "<script>bad()</script>"},
        {"type": "checklist", "title": "建议", "items": ["练习"], "action": "confirm_plan"},
        {"type": "resource_list", "title": "资料", "source_ids": [SOURCE["id"]], "url": SOURCE["url"]},
    ]:
        with pytest.raises(ValidationError):
            TutorProposal.model_validate({**reply, "ui_components": [block]})


def test_sources_resolve_only_from_actual_operation_results():
    spec = {"type": "resource_list", "title": "参考资料", "source_ids": [SOURCE["id"], SOURCE["id"]]}
    assert build_ui_components([spec], [SOURCE])[0]["items"] == [SOURCE]
    for sources in ([], [{**SOURCE, "url": "javascript:bad()"}]):
        with pytest.raises(DomainError, match="未检索"):
            build_ui_components([spec], sources)


async def test_display_is_persisted_scoped_and_does_not_create_learning_tasks(database):
    factory, user, other = database
    conversation = await routes.assistant_conversation(user)
    accepted = await commands.send_message(user.id, conversation["id"], {
        "content": "帮我查 Python 教程", "research_requested": True,
    }, str(uuid4()))
    async with factory() as db, db.begin():
        op = await db.get(Operation, accepted["operation_id"])
        op.result = {"research_sources": [SOURCE], "research_calls": {"test-call": {"status": "succeeded"}}}
    proposal = {
        "kind": "tutor", "message": "参考资料和练习建议。",
        "ui_components": [
            {"type": "resource_list", "title": "资料", "source_ids": [SOURCE["id"]]},
            {"type": "checklist", "title": "练习建议", "items": ["阅读函数章节"]},
        ],
    }
    result = await execution.commit_result(accepted["operation_id"], proposal)
    assert result["status"] == "succeeded"
    assert "research_calls" not in result["result"] and "research_sources" not in result["result"]
    assert result == await execution.commit_result(accepted["operation_id"], proposal)
    page = await routes.messages(conversation["id"], after=0, limit=100, user=user)
    assert page["items"][0]["ui_components"] == []
    blocks = page["items"][1]["ui_components"]
    assert blocks[0]["items"] == [SOURCE]
    assert blocks[1]["items"] == ["阅读函数章节"]
    with pytest.raises(DomainError):
        await routes.messages(conversation["id"], after=0, limit=100, user=other)
    async with factory() as db:
        stored = await db.get(Operation, accepted["operation_id"])
        assert stored.result["research_calls"] == {"test-call": {"status": "succeeded"}}
    context = await execution.load_context(accepted["operation_id"])
    assert context["tasks"] == []


async def test_invalid_ui_cannot_publish_a_message_or_invent_a_source(database):
    factory, user, _ = database
    conversation = await routes.assistant_conversation(user)
    accepted = await commands.send_message(user.id, conversation["id"], {"content": "查资料"}, str(uuid4()))
    with pytest.raises(DomainError):
        await execution.commit_result(accepted["operation_id"], {
            "kind": "tutor", "message": "声称已检索",
            "ui_components": [{"type": "resource_list", "title": "伪造资料", "source_ids": ["invented"]}],
        })
    page = await routes.messages(conversation["id"], after=0, limit=100, user=user)
    assert len(page["items"]) == 1
    async with factory() as db:
        assert (await db.get(Operation, accepted["operation_id"])).status != "succeeded"
