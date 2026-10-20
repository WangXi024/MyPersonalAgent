import json

import httpx
import pytest
from pydantic import ValidationError

from app.agents.schemas import AssessmentProposal, PlanProposal, TutorProposal
from app.integrations.hindsight import HindsightClient, filter_recalled_memories


def test_plan_rejects_missing_milestone_reference():
    with pytest.raises(ValidationError):
        PlanProposal.model_validate(
            {
                "summary": "练习",
                "message": "确认计划",
                "milestones": [{"title": "掌握函数", "criteria": ["能解释"]}],
                "tasks": [
                    {
                        "title": "写函数",
                        "instructions": "实现并解释",
                        "estimated_minutes": 15,
                        "scheduled_date": "2026-10-20",
                        "milestone_index": 3,
                        "criteria": ["能解释"],
                    }
                ],
            }
        )


def test_assessment_cannot_claim_program_verified():
    with pytest.raises(ValidationError):
        AssessmentProposal.model_validate(
            {
                "summary": "已评估",
                "message": "检查完成",
                "submission_id": "a",
                "passed": True,
                "feedback": "代码看起来正确",
                "evidence_type": "program_verified",
            }
        )


def test_tutor_can_clarify_without_a_draft_and_cannot_emit_incomplete_or_privileged_drafts():
    reply = {"summary": "明确目标", "message": "你现在有什么基础？"}
    assert TutorProposal.model_validate(reply).goal_draft is None
    draft = {"title": "学习 Python", "background": "零基础", "outcome": "完成记账工具",
             "criteria": ["能解释核心实现"], "deadline": None}
    for invalid in ({**draft, "background": "  "}, {**draft, "criteria": ["  "]},
                    {**draft, "user_id": "someone-else"}, {**draft, "daily_minutes": 720}):
        with pytest.raises(ValidationError):
            TutorProposal.model_validate({**reply, "goal_draft": invalid})
    with pytest.raises(ValidationError):
        TutorProposal.model_validate({**reply, "goal_draft": draft,
            "follow_up": {"kind": "plan", "instruction": "修改已有目标"}})


def test_recall_excludes_deleted_untraceable_and_old_version_facts():
    sources = [{"id": "a", "version": 2}, {"id": "b", "version": 1, "suppressed": True}]
    results = [
        {"type": "world", "text": "valid", "metadata": {"source_id": "a", "version": "2"}},
        {"type": "world", "text": "old", "metadata": {"source_id": "a", "version": "1"}},
        {"type": "experience", "text": "deleted", "metadata": {"source_id": "b", "version": "1"}},
        {"type": "observation", "text": "derived", "metadata": {"source_id": "a", "version": "2"}},
        {"type": "world", "text": "untraceable"},
    ]
    assert filter_recalled_memories(results, sources) == [{"text": "valid", "source_id": "a", "version": "2"}]


async def test_memory_wire_protocol_uses_versioned_document_and_provenance():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "results": []})

    source = {"id": "source-a", "bank_id": "learning-user-a", "version": 2, "content": "喜欢先练习"}
    async with HindsightClient(transport=httpx.MockTransport(handler)) as client:
        await client.retain(source, "source-a:v2")
        await client.recall(source["bank_id"], "学习习惯")
        await client.delete(source["bank_id"], "source-a:v1")
    body = json.loads(requests[0].content)
    assert body["items"][0]["metadata"] == {"source_id": "source-a", "version": "2"}
    assert body["items"][0]["document_id"] == "source-a:v2"
    assert body["async"] is False
    assert json.loads(requests[1].content)["types"] == ["world", "experience"]
    assert requests[2].method == "DELETE"
    assert requests[2].url.path.endswith("/documents/source-a:v1")


async def test_late_retain_after_tombstone_removes_the_old_document(monkeypatch):
    from app.domain import execution
    from app.integrations import hindsight

    source = {"id": "a", "bank_id": "u", "version": 1, "content": "旧偏好", "suppressed": False, "deleted": False}
    reads = 0
    removed = []

    async def load(source_id):
        nonlocal reads
        reads += 1
        return dict(source) if reads == 1 else {**source, "version": 2, "suppressed": True, "deleted": True}

    async def retain(self, source, document_id):
        assert document_id == "a:v1"

    async def delete(self, bank_id, document_id):
        removed.append((bank_id, document_id))

    async def ack(source_id, version, external_id):
        assert version == 1 and external_id is None
        return {"status": "suppressed"}

    monkeypatch.setattr(execution, "load_memory_source", load)
    monkeypatch.setattr(execution, "ack_memory_sync", ack)
    monkeypatch.setattr(hindsight.HindsightClient, "retain", retain)
    monkeypatch.setattr(hindsight.HindsightClient, "delete", delete)
    assert await hindsight.sync_memory("a") == {"status": "suppressed"}
    assert removed == [("u", "a:v1")]


async def test_transport_retention_preserves_messages_and_current_events(database, monkeypatch):
    from datetime import timedelta

    from sqlalchemy import select

    from app.db.models import BusinessEvent, now
    from app.integrations import maintenance

    factory, user, _ = database
    monkeypatch.setattr(maintenance, "SessionFactory", factory)
    async with factory() as db, db.begin():
        db.add(BusinessEvent(user_id=user.id, seq=1, type="old", entity_id=user.id,
                            summary="old", created_at=now() - timedelta(days=31)))
        db.add(BusinessEvent(user_id=user.id, seq=2, type="current", entity_id=user.id,
                            summary="current"))
    result = await maintenance.prune_expired_records()
    assert result["events"] == 1
    async with factory() as db:
        assert list(await db.scalars(select(BusinessEvent.seq))) == [2]


def test_provider_sdk_cannot_retry_behind_the_operation_budget():
    from app.agents.learning import AGENTS
    assert all(agent.model.wrapped.provider.client.max_retries == 0 for agent in AGENTS.values())


@pytest.mark.parametrize("status,non_retryable", [(401, True), (503, False)])
async def test_provider_error_body_cannot_enter_workflow_failure(monkeypatch, status, non_retryable):
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.openai import OpenAIChatModel
    from temporalio.exceptions import ApplicationError

    from app.agents.learning import AGENTS
    from app.domain import execution

    reservations = []

    async def reserve(operation_id, limit):
        reservations.append((operation_id, limit))
        return {"model_alias": "learning-agent"}

    async def fail(*args, **kwargs):
        raise ModelHTTPError(status, "test-model", body="private-provider-payload-test-only")

    monkeypatch.setattr(execution, "reserve_model_request", reserve)
    monkeypatch.setattr(OpenAIChatModel, "request", fail)
    with pytest.raises(ApplicationError) as caught:
        await AGENTS["tutor"].model.request([], {"learning_operation_id": "test-operation"},
                                           ModelRequestParameters())
    assert reservations == [("test-operation", 6)]
    assert caught.value.non_retryable is non_retryable
    assert "private-provider-payload" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
