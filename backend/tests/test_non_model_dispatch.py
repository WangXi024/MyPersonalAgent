"""Non-model operations bypass the learning inbox and preserve cleanup routing."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from app.api.routes import cancel_operation
from app.db.models import Operation, Outbox
from app.dispatcher import deliver


class Client:
    def __init__(self, closed=False):
        self.closed = closed
        self.starts, self.signals, self.handles = [], [], []

    async def start_workflow(self, handler, operation_id, **options):
        self.starts.append((handler, operation_id, options))

    def get_workflow_handle(self, identifier):
        self.handles.append(identifier)
        return self

    async def signal(self, handler):
        self.signals.append(handler)
        if self.closed:
            raise RPCError("closed synthetic workflow", RPCStatusCode.NOT_FOUND, b"")


@pytest.mark.parametrize("kind,prefix,timeout", [("material_parse", "material:", 420),
                                               ("verification_run", "verification:", 87180)])
async def test_non_model_dispatch_uses_dedicated_workflow(kind, prefix, timeout):
    client = Client()
    operation_id = str(uuid4())
    await deliver(client, {"kind": "temporal", "payload": {"type": kind, "operation_id": operation_id}})
    assert len(client.starts) == 1 and not client.signals
    _, received, options = client.starts[0]
    assert received == operation_id and options["id"] == prefix + operation_id
    assert options["execution_timeout"].total_seconds() == timeout
    assert options["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY


@pytest.mark.parametrize("kind", ["verification_run", "material_parse"])
async def test_generic_cancel_queues_dedicated_cancel_command(database, kind):
    factory, user, _ = database
    async with factory() as db, db.begin():
        op = Operation(user_id=user.id, kind=kind, idempotency_key=str(uuid4()), request_hash="0" * 64)
        db.add(op)
        await db.flush()
        identifier = op.id
    await cancel_operation(identifier, user)
    await cancel_operation(identifier, user)
    async with factory() as db:
        rows = list(await db.scalars(select(Outbox).where(Outbox.operation_id == identifier)))
        assert len(rows) == 1 and rows[0].payload["type"] == kind + "_cancelled"
        assert rows[0].payload["operation_id"] == identifier
        assert (await db.get(Operation, identifier)).status == "cancelled"


@pytest.mark.parametrize("closed", [False, True])
async def test_verification_cancel_can_resume_only_persisted_cleanup(closed):
    client = Client(closed=closed)
    identifier = str(uuid4())
    await deliver(client, {"kind": "temporal", "payload": {
        "type": "verification_run_cancelled", "operation_id": identifier}})
    assert client.handles == ["verification:" + identifier] and len(client.signals) == 1
    assert len(client.starts) == int(closed)
    if closed:
        assert client.starts[0][1] == identifier
        assert client.starts[0][2]["id"] == "verification:" + identifier
        assert client.starts[0][2]["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
