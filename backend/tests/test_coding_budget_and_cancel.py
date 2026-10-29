"""Model reservation and generic cancellation must respect Coder's separate lifecycle."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api import routes
from app.db.models import Operation, Outbox, Task, User
from app.domain import coding_runs as domain, execution
from app.domain.common import DomainError
from app.integrations.settings import RuntimeSettings
from test_sandbox_execution import saved_submission


async def create_run(database, monkeypatch):
    monkeypatch.setattr(domain, "runtime_settings", lambda: RuntimeSettings(coding_enabled=True))
    submission_id, original, task_id = await saved_submission(database)
    payload = {key: original[key] for key in ("expected_version", "expected_goal_revision", "attachment_id")}
    item = await domain.create_run(database[1].id, submission_id,
        {**payload, "instruction": "解释并检查这段已保存代码"}, str(uuid4()))
    return item, task_id


@pytest.mark.parametrize("changed", ["submission", "user", "cancel"])
async def test_model_reservation_checks_coding_scope_in_the_same_transaction(database, monkeypatch, changed):
    item, task_id = await create_run(database, monkeypatch)
    async with database[0]() as db, db.begin():
        if changed == "submission":
            (await db.get(Task, task_id)).active_submission_id = None
        elif changed == "user":
            (await db.get(User, database[1].id)).active = False
        else:
            (await db.get(Operation, item["operation_id"])).status = "cancelled"
    with pytest.raises(DomainError):
        await execution.reserve_model_request(item["operation_id"])
    async with database[0]() as db:
        assert (await db.get(Operation, item["operation_id"])).model_requests == 0


async def test_generic_cancel_sends_the_coder_signal_instead_of_the_goal_signal(database, monkeypatch):
    item, _ = await create_run(database, monkeypatch)
    result = await routes.cancel_operation(item["operation_id"], database[1])
    assert result["status"] == "cancelled"
    async with database[0]() as db:
        row = await db.scalar(select(Outbox).where(Outbox.dedupe_key == "operation-cancel:" + item["operation_id"]))
        assert row.payload["type"] == "coding_run_cancelled"
    await routes.cancel_operation(item["operation_id"], database[1])
    async with database[0]() as db:
        rows = list(await db.scalars(select(Outbox).where(
            Outbox.dedupe_key == "operation-cancel:" + item["operation_id"])))
        assert len(rows) == 1
