"""Run only against an explicitly configured PostgreSQL database, with a fresh schema per test."""

import asyncio
import os

import pytest
from sqlalchemy import select

from app.db.models import BusinessEvent, DailyBudget, EventStream, Goal, Message, Operation
from app.domain import commands, execution
from app.domain.common import DomainError, lock_user
from test_domain import active_goal, key, plan_result

pytestmark = pytest.mark.skipif(
    not os.environ.get("APP_TEST_DATABASE_URL"), reason="PostgreSQL URL not supplied"
)


@pytest.mark.parametrize("different_payloads", [False, True])
async def test_goal_draft_concurrent_confirmation_creates_one_goal(database, different_payloads):
    from sqlalchemy import func

    from app.db.models import Outbox
    from test_domain import pending_goal_draft

    factory, user, other = database
    conversation, source = await pending_goal_draft(user)
    payloads = [
        {
            **source["result"]["goal_draft"],
            "title": f"目标 {index}" if different_payloads else "相同目标",
        }
        for index in range(6)
    ]
    results = await asyncio.gather(
        *(commands.accept_goal_draft(user.id, source["id"], key(), payload) for payload in payloads),
        return_exceptions=True,
    )
    successes = [item for item in results if not isinstance(item, Exception)]
    assert len(successes) == (1 if different_payloads else 6)
    assert len({item["resource_id"] for item in successes}) == 1
    assert all(
        isinstance(item, DomainError) and item.status == 409
        for item in results
        if isinstance(item, Exception)
    )
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Goal)) == 1
        assert (
            await db.scalar(select(func.count()).select_from(Operation).where(Operation.kind == "plan")) == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.operation_id == successes[0]["operation_id"])
            )
            == 1
        )


async def test_concurrent_goal_confirmation_cannot_overbook(database):
    factory, user, other = database
    plans = []
    for index in range(2):
        created = await commands.create_goal(user.id, key(), {"title": str(index), "outcome": "Learn"})
        result = await execution.commit_result(created["operation_id"], plan_result())
        plans.append(result["result"]["resource_id"])
    results = await asyncio.gather(
        *(commands.accept_plan(user.id, plan, 1, True, key()) for plan in plans), return_exceptions=True
    )
    assert sum(isinstance(result, DomainError) for result in results) == 1
    async with factory() as db:
        row = await db.scalar(select(DailyBudget).where(DailyBudget.user_id == user.id))
        assert row.reserved_minutes == 20 and row.authorized_minutes == 30


async def test_concurrent_same_command_has_one_effect(database):
    factory, user, other = database
    request_key = key()
    payload = {"title": "Python", "outcome": "Tool"}
    results = await asyncio.gather(*(commands.create_goal(user.id, request_key, payload) for _ in range(8)))
    assert len({r["operation_id"] for r in results}) == 1
    async with factory() as db:
        goals = list((await db.scalars(select(Goal))).all())
        assert len(goals) == 1


async def test_event_sequences_follow_committed_transactions(database):
    factory, user, other = database
    results = await asyncio.gather(
        *(commands.create_goal(user.id, key(), {"title": str(i), "outcome": "Tool"}) for i in range(12))
    )
    async with factory() as db:
        stream = await db.get(EventStream, user.id)
        events = list(
            (
                await db.scalars(
                    select(BusinessEvent).where(BusinessEvent.user_id == user.id).order_by(BusinessEvent.seq)
                )
            ).all()
        )
        assert [e.seq for e in events] == list(range(1, stream.last_seq + 1))
        assert len(results) == 12 and stream.last_seq == 24


async def test_outbox_parallel_claims_are_disjoint(database):
    factory, user, other = database
    for index in range(8):
        await commands.create_goal(user.id, key(), {"title": str(index), "outcome": "Tool"})
    claims = await asyncio.gather(execution.claim_outbox("a", 4), execution.claim_outbox("b", 4))
    ids = [row["id"] for group in claims for row in group]
    assert len(ids) == len(set(ids)) == 8


async def test_pause_commits_before_blocked_result_no_publication(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    recommendation = await commands.request_operation(user.id, key(), goal["id"], "recommendation", {})
    async with factory() as db:
        async with db.begin():
            await lock_user(db, user.id)
            row = await db.get(Goal, goal["id"])
            row.status = "paused"
            row.control_revision += 1
            result_task = asyncio.create_task(
                execution.commit_result(
                    recommendation["operation_id"], {"kind": "recommendation", "message": "late"}
                )
            )
            await asyncio.sleep(0.05)
            assert not result_task.done()
        result = await result_task
    assert result["status"] == "superseded"
    async with factory() as db:
        assert (
            await db.scalar(select(Message).where(Message.operation_id == recommendation["operation_id"]))
            is None
        )


async def test_cancel_fences_waiting_commit(database):
    factory, user, other = database
    goal, task, plan = await active_goal(user)
    recommendation = await commands.request_operation(user.id, key(), goal["id"], "recommendation", {})
    async with factory() as db:
        async with db.begin():
            await lock_user(db, user.id)
            operation = await db.get(Operation, recommendation["operation_id"])
            operation.status = "cancelled"
            result_task = asyncio.create_task(
                execution.commit_result(operation.id, {"kind": "recommendation", "message": "late"})
            )
            await asyncio.sleep(0.05)
        result = await result_task
    assert result["status"] == "cancelled"


async def test_concurrent_proactive_results_share_daily_quota(database, monkeypatch):
    from types import SimpleNamespace
    from app.db.models import User

    factory, user, other = database
    async with factory() as db, db.begin():
        account = await db.get(User, user.id)
        account.daily_minutes = 60
        account.quiet_start = account.quiet_end = 0
    first_goal, _, _ = await active_goal(user)
    second_goal, _, _ = await active_goal(user)
    monkeypatch.setattr(
        execution, "get_settings", lambda: SimpleNamespace(proactive_daily_limit=1, proactive_gap_hours=4)
    )
    operations = [
        await commands.request_operation(user.id, key(), goal["id"], "recommendation", {}, proactive=True)
        for goal in (first_goal, second_goal)
    ]
    results = await asyncio.gather(
        *(
            execution.commit_result(
                op["operation_id"], {"kind": "recommendation", "message": "今天的任务已准备"}
            )
            for op in operations
        )
    )
    assert sorted(r["status"] for r in results) == ["succeeded", "superseded"]


async def test_model_call_budget_is_atomic_across_activity_retries(database):
    factory, user, other = database
    operation = await commands.create_goal(user.id, key(), {"title": "Python", "outcome": "Tool"})
    results = await asyncio.gather(
        *(execution.reserve_model_request(operation["operation_id"]) for _ in range(8)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, DomainError) for r in results) == 2
    async with factory() as db:
        op = await db.get(Operation, operation["operation_id"])
        assert op.model_requests == 6


async def test_bootstrap_snapshot_and_cursor_have_no_connection_gap(database, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from app.api import routes

    factory, user, other = database
    cursor_read = asyncio.Event()
    continue_snapshot = asyncio.Event()

    class PausedSnapshotSession(AsyncSession):
        async def get(self, entity, ident, **kwargs):
            result = await super().get(entity, ident, **kwargs)
            if entity is EventStream:
                cursor_read.set()
                await continue_snapshot.wait()
            return result

    snapshot_factory = async_sessionmaker(
        factory.kw["bind"], class_=PausedSnapshotSession, expire_on_commit=False
    )
    monkeypatch.setattr(routes, "SessionFactory", snapshot_factory)
    snapshot_task = asyncio.create_task(routes.bootstrap(user))
    await cursor_read.wait()
    command = await commands.create_goal(
        user.id, key(), {"title": "Arrived during snapshot", "outcome": "Tool"}
    )
    continue_snapshot.set()
    snapshot = await snapshot_task
    assert snapshot["cursor"] == 0 and snapshot["goals"] == []
    async with factory() as db:
        replay = list(
            (
                await db.scalars(
                    select(BusinessEvent).where(
                        BusinessEvent.user_id == user.id, BusinessEvent.seq > snapshot["cursor"]
                    )
                )
            ).all()
        )
        assert any(event.goal_id == command["resource_id"] for event in replay)


async def test_resume_requeues_current_assessment_without_recounting_study(database):
    from sqlalchemy import func
    from app.db.models import Evaluation, StudyRecord, Task

    factory, user, other = database
    goal, task, plan = await active_goal(user)
    submitted = await commands.submit(
        user.id,
        task["id"],
        {"text": "return 3", "expected_version": 1, "attempt_id": key(), "minutes": 12, "estimated": False},
        key(),
    )
    await execution.mark_running(submitted["operation_id"])
    paused = await commands.control_goal(user.id, goal["id"], "pause", 1, key())
    resume_key = key()
    await commands.control_goal(user.id, goal["id"], "resume", paused["control_revision"], resume_key)
    await commands.control_goal(user.id, goal["id"], "resume", paused["control_revision"], resume_key)
    async with factory() as db:
        pending = list(
            (
                await db.scalars(
                    select(Operation).where(
                        Operation.goal_id == goal["id"],
                        Operation.kind == "assessment",
                        Operation.status == "accepted",
                    )
                )
            ).all()
        )
        assert len(pending) == 1
        assert pending[0].payload["submission_id"] == submitted["resource_id"]
        resumed_id = pending[0].id
    old = await execution.commit_result(
        submitted["operation_id"], {"kind": "assessment", "passed": True, "feedback": "迟到旧结果"}
    )
    assert old["status"] == "superseded"
    await execution.commit_result(
        resumed_id, {"kind": "assessment", "passed": True, "feedback": "恢复后评估"}
    )
    async with factory() as db:
        assert (await db.get(Task, task["id"])).status == "completed"
        assert await db.scalar(select(func.count()).select_from(StudyRecord)) == 1
        assert await db.scalar(select(func.count()).select_from(Evaluation)) == 1
        row = await db.scalar(select(DailyBudget).where(DailyBudget.user_id == user.id))
        assert row.consumed_minutes == 12 and row.reserved_minutes == 0
