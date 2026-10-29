from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.dispatcher import deliver
from app.workflows.sandbox import SandboxExecutionWorkflow


async def test_sandbox_outbox_routes_to_its_own_workflow_without_a_model_or_goal_loop():
    client = SimpleNamespace(start_workflow=AsyncMock())
    item = {"id": "outbox-1", "kind": "temporal", "payload": {
        "type": "sandbox_execution", "operation_id": "execution-op", "goal_id": "goal-1", "user_id": "user-1",
    }}
    await deliver(client, item)
    arguments = client.start_workflow.call_args
    assert arguments.args == (SandboxExecutionWorkflow.run, "execution-op")
    assert arguments.kwargs["id"] == "sandbox-execution-op"


async def test_sandbox_cancel_signals_the_existing_run_without_starting_another():
    handle = SimpleNamespace(signal=AsyncMock())
    client = SimpleNamespace(get_workflow_handle=lambda identity: handle, start_workflow=AsyncMock())
    await deliver(client, {"id": "outbox-2", "kind": "temporal", "payload": {
        "type": "sandbox_execution_cancelled", "operation_id": "execution-op",
    }})
    handle.signal.assert_awaited_once_with(SandboxExecutionWorkflow.cancel)
    client.start_workflow.assert_not_called()
