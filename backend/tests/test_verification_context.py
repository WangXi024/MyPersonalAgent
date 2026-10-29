"""Only currently applicable sample receipts reach teaching UI, context and reports."""

from uuid import uuid4

from sqlalchemy import select

from app.agents.ui_v3 import progress_snapshot
from app.db.models import Conversation, Operation, Task
from app.domain import commands, execution, reports, verification
from app.integrations.settings import runtime_settings
from test_verification import Node, drain, saved_case


async def test_verified_sample_scope_matches_context_progress_and_report(database, monkeypatch):
    factory, user, _ = database
    monkeypatch.setattr(runtime_settings(), "verification_enabled", True)
    monkeypatch.setattr(reports, "SessionFactory", factory)
    run, _, _, _ = await saved_case(database)
    node = Node(factory)
    monkeypatch.setattr(verification, "VerificationTransport", node.client)
    assert (await drain(run["operation_id"]))["status"] == "passed"
    async with factory() as db:
        operation = await db.get(Operation, run["operation_id"])
        snapshot = await progress_snapshot(db, operation)
        assert snapshot["evidence"] == {"ai_review": 0, "verified_tests": 1}
        conversation_id = await db.scalar(select(Conversation.id).where(Conversation.goal_id == run["goal_id"]))
    sent = await commands.send_message(user.id, conversation_id, {"content": "查看当前样例验证记录"}, str(uuid4()))
    context = await execution.load_context(sent["operation_id"])
    assert context["verified_sample_tests"][0]["id"] == run["id"]
    assert context["verified_sample_tests"][0]["passed"] is True
    assert not {"source", "stdout", "stderr", "expected_stdout"} & context["verified_sample_tests"][0].keys()
    report = (await reports.render_learning_report(user.id, run["goal_id"])).content.decode()
    assert run["source_sha256"] in report and "tests_verified=true" in report
    async with factory() as db, db.begin():
        task = await db.get(Task, run["task_id"])
        task.requirements_version += 1
    async with factory() as db:
        operation = await db.get(Operation, run["operation_id"])
        assert (await progress_snapshot(db, operation))["evidence"]["verified_tests"] == 0
    assert (await execution.load_context(sent["operation_id"]))["verified_sample_tests"] == []
    assert "tests_verified=true" not in (await reports.render_learning_report(user.id, run["goal_id"])).content.decode()
