from copy import deepcopy
import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.agents.schemas_v3 import TutorProposalV3
from app.api import routes
from app.db.models import DailyBudget, Evaluation, Message, Operation, Task
from app.domain import commands, execution, practice
from app.domain.common import DomainError, public_operation
from app.main import app

QUIZ = {"type": "quiz", "title": "函数自测", "questions": [{"prompt": "函数用什么返回结果？", "choices": [
    {"id": "a", "text": "return"}, {"id": "b", "text": "print"}], "correct_choice_id": "a", "explanation": "return 将结果交给调用方。"}]}


async def make_quiz(database, *, goal=None):
    _, user, _ = database
    if goal:
        async with database[0]() as db:
            from app.db.models import Conversation
            conversation_id = await db.scalar(select(Conversation.id).where(Conversation.goal_id == goal["id"]))
    else:
        conversation_id = (await routes.assistant_conversation(user))["id"]
    created = await commands.send_message(user.id, conversation_id, {"content": "给我一个练习"}, str(uuid4()))
    proposal = {"kind": "tutor", "message": "请完成自测。", "ui_components": [deepcopy(QUIZ)], "teaching_version": "teaching-v1"}
    result = await execution.commit_result(created["operation_id"], proposal)
    return created["operation_id"], conversation_id, result


@pytest.mark.parametrize("change", ["duplicate", "missing_answer", "html", "progress_value"])
def test_strict_v3_components_reject_untrusted_shape(change):
    block = deepcopy(QUIZ)
    if change == "duplicate":
        block["questions"][0]["choices"][1]["id"] = "a"
    elif change == "missing_answer":
        block["questions"][0]["correct_choice_id"] = "d"
    elif change == "html":
        block["html"] = "<script>bad()</script>"
    else:
        block = {"type": "progress", "title": "伪造进度", "passed": True, "snapshot": {"completed": 100}}
    with pytest.raises(ValidationError):
        TutorProposalV3.model_validate({"summary": "test", "message": "test", "ui_components": [block]})


async def test_quiz_keys_are_private_attempts_are_idempotent_scoped_and_not_learning_evidence(database):
    factory, user, other = database
    operation_id, conversation_id, result = await make_quiz(database)
    block = result["result"]["ui_components"][0]
    assert "private_quizzes" not in result["result"] and "correct_choice_id" not in str(block)
    assert "explanation" not in str(block) and block["attempt_count"] == 0 and block["practice_only"]
    answers = [{"question_id": "q1", "choice_id": "b"}]
    attempt = await practice.submit_attempt(user.id, operation_id, "quiz-1", answers, "attempt-one")
    assert not attempt["feedback"][0]["correct"] and attempt["feedback"][0]["correct_choice_id"] == "a"
    assert await practice.submit_attempt(user.id, operation_id, "quiz-1", answers, "attempt-one") == attempt
    with pytest.raises(DomainError) as conflict:
        await practice.submit_attempt(user.id, operation_id, "quiz-1", [{"question_id": "q1", "choice_id": "a"}], "attempt-one")
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(DomainError) as denied:
        await practice.submit_attempt(other.id, operation_id, "quiz-1", answers, "attempt-foreign")
    assert denied.value.status == 404
    page = await routes.messages(conversation_id, after=0, limit=100, user=user)
    assert page["items"][1]["ui_components"][0]["attempt"] == attempt
    async with factory() as db:
        op = await db.get(Operation, operation_id)
        public = public_operation(op)
        assert "quiz_request_keys" not in public["result"] and "private_quizzes" not in public["result"]
        assert public["result"]["ui_components"][0]["attempt_count"] == 1
        assert await db.scalar(select(func.count()).select_from(Message)) == 2
        for model in (Task, Evaluation, DailyBudget):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_quiz_validates_complete_choices_and_has_bounded_retakes(database):
    _, user, _ = database
    operation_id, _, _ = await make_quiz(database)
    for answers in [[], [{"question_id": "q2", "choice_id": "a"}], [{"question_id": "q1", "choice_id": "d"}],
                    [{"question_id": "q1", "choice_id": "a"}] * 2]:
        with pytest.raises(DomainError):
            await practice.submit_attempt(user.id, operation_id, "quiz-1", answers, str(uuid4()))
    for index in range(5):
        result = await practice.submit_attempt(user.id, operation_id, "quiz-1", [{"question_id": "q1", "choice_id": "a"}], f"try-{index}")
        assert result["attempt_number"] == index + 1
    with pytest.raises(DomainError) as capped:
        await practice.submit_attempt(user.id, operation_id, "quiz-1", [{"question_id": "q1", "choice_id": "a"}], "try-six")
    assert capped.value.status == 429


async def test_progress_is_server_derived_and_global_scope_cannot_claim_goal_progress(database):
    from test_domain import active_goal
    factory, user, _ = database
    goal, _, _ = await active_goal(user)
    async with factory() as db:
        from app.db.models import Conversation
        conversation_id = await db.scalar(select(Conversation.id).where(Conversation.goal_id == goal["id"]))
    accepted = await commands.send_message(user.id, conversation_id, {"content": "我的进度"}, str(uuid4()))
    result = await execution.commit_result(accepted["operation_id"], {"kind": "tutor", "message": "当前进度。",
        "teaching_version": "teaching-v1", "ui_components": [{"type": "progress", "title": "任务进度"}]})
    snapshot = result["result"]["ui_components"][0]["snapshot"]
    assert snapshot["goal_id"] == goal["id"] and snapshot["tasks"]["total"] == snapshot["tasks"]["ready"] == 1
    assert snapshot["evidence"] == {"ai_review": 0, "verified_tests": 0}
    global_id = (await routes.assistant_conversation(user))["id"]
    global_op = await commands.send_message(user.id, global_id, {"content": "进度"}, str(uuid4()))
    with pytest.raises(DomainError):
        await execution.commit_result(global_op["operation_id"], {"kind": "tutor", "message": "不应发布",
            "teaching_version": "teaching-v1", "ui_components": [{"type": "progress", "title": "伪造"}]})


async def test_quiz_rest_uses_existing_login_csrf_and_strict_body(database):
    operation_id, _, _ = await make_quiz(database)
    path = f"/api/v1/operations/{operation_id}/quizzes/quiz-1/attempts"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        body = {"answers": [{"question_id": "q1", "choice_id": "a"}]}
        assert (await client.post(path, json=body)).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"username": "learner", "password": "test-only-long-password"},
                                  headers={"Origin": "http://localhost:8080"})
        headers = {"Origin": "http://localhost:8080", "X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": "quiz-http-once"}
        assert (await client.post(path, json=body)).status_code == 403
        assert (await client.post(path, json={**body, "passed": True}, headers=headers)).status_code == 422
        saved = await client.post(path, json=body, headers=headers)
        assert saved.status_code == 200 and saved.json()["practice_only"]
        restored = await client.get(f"/api/v1/operations/{operation_id}")
        assert restored.json()["result"]["ui_components"][0]["attempt"] == saved.json()


async def test_progress_skills_require_current_plan_submission_rubric_and_valid_evidence(database):
    from app.agents.ui_v3 import progress_snapshot
    from app.db.models import Goal
    from test_domain import active_goal
    factory, user, _ = database
    goal, task, _ = await active_goal(user)
    submission = await commands.submit(user.id, task["id"], {"text": "print(3)", "attachment_ids": [],
        "expected_version": task["state_version"], "attempt_id": str(uuid4()), "minutes": 1, "estimated": False}, str(uuid4()))
    await execution.commit_result(submission["operation_id"], {"kind": "assessment", "passed": False,
        "feedback": "应返回结果。", "evidence_type": "ai_review", "skills": [{"name": "函数", "passed": False}]})
    async with factory() as db, db.begin():
        op = await db.get(Operation, submission["operation_id"])
        current_task = await db.get(Task, task["id"])
        evaluation = await db.scalar(select(Evaluation).where(Evaluation.operation_id == op.id))
        original = await progress_snapshot(db, op)
        assert original["skills"] == {"total": 1, "needs_review": 1} and original["evidence"]["ai_review"] == 1
        for entity, field, changed in [(evaluation, "valid", False), (current_task, "active_submission_id", None),
            (current_task, "requirements_version", current_task.requirements_version + 1),
            (await db.get(Goal, goal["id"]), "active_plan_id", None)]:
            previous = getattr(entity, field)
            setattr(entity, field, changed)
            await db.flush()
            snapshot = await progress_snapshot(db, op)
            assert snapshot["skills"] == {"total": 0, "needs_review": 0} and snapshot["evidence"]["ai_review"] == 0
            setattr(entity, field, previous)
            await db.flush()


@pytest.mark.skipif(not os.getenv("APP_TEST_DATABASE_URL"), reason="Real PostgreSQL user-row lock required")
async def test_quiz_parallel_retries_do_not_duplicate_or_lose_attempts(database):
    operation_id, _, _ = await make_quiz(database)
    user_id = database[1].id
    answers = [{"question_id": "q1", "choice_id": "a"}]
    duplicate = await asyncio.gather(*(practice.submit_attempt(user_id, operation_id, "quiz-1", answers, "parallel-once") for _ in range(2)))
    assert duplicate[0] == duplicate[1] and duplicate[0]["attempt_number"] == 1
    distinct = await asyncio.gather(*(practice.submit_attempt(user_id, operation_id, "quiz-1", answers, f"parallel-{i}") for i in range(2)))
    assert {attempt["attempt_number"] for attempt in distinct} == {2, 3}
