"""Concurrency checks require an explicitly supplied isolated PostgreSQL test database."""
import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.models import Operation, Outbox, SandboxExecution
from app.domain import sandbox_execution as domain
from app.domain.common import DomainError
from app.integrations.settings import RuntimeSettings
from test_sandbox_execution import saved_submission

pytestmark = pytest.mark.skipif(not os.getenv("APP_TEST_DATABASE_URL"), reason="PostgreSQL URL not supplied")


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_execution_creation_is_idempotent_and_cannot_bypass_limit(database, monkeypatch, same_key):
    factory, user, _ = database
    settings = RuntimeSettings(sandbox_enabled=True, sandbox_max_concurrent_per_user=1)
    monkeypatch.setattr(domain, "runtime_settings", lambda: settings)
    submission, payload, _ = await saved_submission(database)
    identity = str(uuid4())
    results = await asyncio.gather(*(domain.create_execution(user.id, submission, payload,
        identity if same_key else str(uuid4())) for _ in range(5)), return_exceptions=True)
    successes = [result for result in results if not isinstance(result, Exception)]
    assert len(successes) == (5 if same_key else 1)
    assert len({result["id"] for result in successes}) == 1
    assert all(isinstance(error, DomainError) and error.status == 429 for error in results if isinstance(error, Exception))
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(SandboxExecution)) == 1
        assert await db.scalar(select(func.count()).select_from(Operation).where(Operation.kind == "sandbox_execution")) == 1
        assert await db.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.dedupe_key == f"sandbox:{successes[0]['id']}")) == 1


async def test_parallel_cancels_with_new_keys_produce_one_control_outbox(database, monkeypatch):
    factory, user, _ = database
    monkeypatch.setattr(domain, "runtime_settings", lambda: RuntimeSettings(sandbox_enabled=True))
    submission, payload, _ = await saved_submission(database)
    item = await domain.create_execution(user.id, submission, payload, str(uuid4()))
    results = await asyncio.gather(*(domain.request_cancel(user.id, item["id"], str(uuid4())) for _ in range(5)))
    assert all(result["cancel_requested"] for result in results)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.dedupe_key == f"sandbox-cancel:{item['id']}")) == 1
