from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.dispatcher import deliver
from app.integrations.settings import runtime_settings
from app.workflows.coding import CodingWorkflow


async def test_coder_uses_an_independent_workflow_with_time_for_cleanup():
    client = SimpleNamespace(start_workflow=AsyncMock())
    await deliver(client, {"id": "coding-outbox", "kind": "temporal", "payload": {
        "type": "coding_run", "operation_id": "coding-operation", "goal_id": "goal", "user_id": "user"}})
    arguments = client.start_workflow.call_args
    assert arguments.args == (CodingWorkflow.run, "coding-operation")
    assert arguments.kwargs["id"] == "coding-coding-operation"
    assert arguments.kwargs["execution_timeout"].total_seconds() > runtime_settings().coding_deadline_seconds + 86400


async def test_coder_cancel_only_signals_the_existing_workflow():
    handle = SimpleNamespace(signal=AsyncMock())
    client = SimpleNamespace(get_workflow_handle=lambda _: handle, start_workflow=AsyncMock())
    await deliver(client, {"id": "coding-cancel-outbox", "kind": "temporal", "payload": {
        "type": "coding_run_cancelled", "operation_id": "coding-operation"}})
    handle.signal.assert_awaited_once_with(CodingWorkflow.cancel)
    client.start_workflow.assert_not_called()
