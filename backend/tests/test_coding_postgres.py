"""Real PostgreSQL row locks protect one Coder workspace per user's concurrent budget."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import CodingRun, Operation, Outbox
from app.domain import coding_runs as domain
from app.domain.common import DomainError
from app.integrations.settings import RuntimeSettings
from test_sandbox_execution import saved_submission

pytestmark = pytest.mark.skipif(not os.getenv("APP_TEST_DATABASE_URL"), reason="PostgreSQL URL not supplied")


@pytest.mark.parametrize("same_key", [False, True])
async def test_coder_concurrent_creation_cannot_bypass_workspace_limit(database, monkeypatch, same_key):
    factory, user, _ = database
    monkeypatch.setattr(domain, "runtime_settings", lambda: RuntimeSettings(coding_enabled=True,
                                                                         coding_max_concurrent_per_user=1))
    submission, original, _ = await saved_submission(database)
    payload = {key: original[key] for key in ("expected_version", "expected_goal_revision", "attachment_id")}
    payload["instruction"] = "检查已保存的Python代码"
    identity = str(uuid4())
    results = await asyncio.gather(*(domain.create_run(user.id, submission, payload,
        identity if same_key else str(uuid4())) for _ in range(5)), return_exceptions=True)
    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == (5 if same_key else 1)
    assert len({result["id"] for result in successes}) == 1
    assert all(isinstance(error, DomainError) and error.status == 429 for error in results if isinstance(error, Exception))
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(CodingRun)) == 1
        assert await db.scalar(select(func.count()).select_from(Operation).where(Operation.kind == "coding_run")) == 1
        assert await db.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.dedupe_key == "coding:" + successes[0]["id"])) == 1


async def test_coder_parallel_cancel_only_enqueues_one_signal(database, monkeypatch):
    factory, user, _ = database
    monkeypatch.setattr(domain, "runtime_settings", lambda: RuntimeSettings(coding_enabled=True))
    submission, original, _ = await saved_submission(database)
    payload = {key: original[key] for key in ("expected_version", "expected_goal_revision", "attachment_id")}
    item = await domain.create_run(user.id, submission, {**payload, "instruction": "检查Python代码"}, str(uuid4()))
    results = await asyncio.gather(*(domain.request_cancel(user.id, item["id"], str(uuid4())) for _ in range(5)))
    assert all(result["status"] == "cancelling" for result in results)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.dedupe_key == "coding-cancel:" + item["id"])) == 1
