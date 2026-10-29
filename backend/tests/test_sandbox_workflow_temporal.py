import os
from datetime import timedelta
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("TEMPORAL_TEST_ADDRESS"), reason="Set TEMPORAL_TEST_ADDRESS")


@pytest.mark.parametrize("cancel_before_submit,ack_failures", [(False, 0), (True, 0), (False, 3)])
async def test_sandbox_durable_result_order_and_replay(monkeypatch, cancel_before_submit, ack_failures):
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker

    from app.domain import sandbox_execution
    from app.integrations.sandbox import SandboxRPCError
    from app.workflows.sandbox import SandboxExecutionWorkflow
    from app.workflows.sandbox_activities import SANDBOX_ACTIVITIES

    calls = []
    state = {"execution_id": "synthetic-execution", "operation_id": "synthetic-op", "job_id": "synthetic-job",
             "status": "accepted", "phase": "", "terminal": False, "submitted": False,
             "result_saved": False, "needs_ack": False, "cleanup_done": False, "delivery_done": False,
             "cancel_requested": cancel_before_submit, "remaining_seconds": 60}

    async def prepare(operation_id):
        calls.append("prepare")
        return dict(state)

    async def submit(operation_id):
        calls.append("submit")
        state.update(submitted=True, phase="running", status="running")
        return dict(state)

    async def poll(operation_id):
        calls.append("poll")
        if state["delivery_done"]:
            state.update(cleanup_done=True, terminal=True)
        else:
            state.update(phase="finished")
        return dict(state)

    async def collect(operation_id):
        calls.append("collect")
        assert state["phase"] == "finished" and not state["cleanup_done"]
        state.update(status="succeeded", result_saved=True, needs_ack=True)
        return dict(state)

    async def ack(operation_id):
        calls.append("ack")
        assert state["result_saved"]
        if calls.count("ack") <= ack_failures:
            raise SandboxRPCError("UNAVAILABLE")
        state.update(needs_ack=False, delivery_done=True)
        return dict(state)

    async def cancel(operation_id):
        calls.append("cancel")
        state.update(status="cancelled", phase="finished", terminal=True, cleanup_done=True)
        return dict(state)

    async def fail(operation_id, code):
        raise AssertionError(f"Unexpected failure: {code}")

    for name, function in {"prepare_execution": prepare, "submit_execution": submit, "poll_execution": poll,
                           "collect_execution": collect, "ack_execution": ack, "cancel_execution": cancel,
                           "fail_execution": fail}.items():
        monkeypatch.setattr(sandbox_execution, name, function)
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
                                  namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"))
    identity = f"test-sandbox-{uuid4()}"
    async with Worker(client, task_queue=identity, workflows=[SandboxExecutionWorkflow], activities=SANDBOX_ACTIVITIES):
        result = await client.execute_workflow(SandboxExecutionWorkflow.run, "synthetic-op", id=identity,
            task_queue=identity, execution_timeout=timedelta(seconds=45))
    assert result["terminal"] and result["cleanup_done"]
    if cancel_before_submit:
        assert calls == ["prepare", "cancel"]
    elif not ack_failures:
        assert calls == ["prepare", "submit", "poll", "collect", "ack", "poll"]
    else:
        assert calls.count("submit") == calls.count("collect") == 1
        assert calls.count("ack") == ack_failures + 1
        assert calls.count("prepare") == 2
    previous = list(calls)
    history = await client.get_workflow_handle(identity).fetch_history()
    await Replayer(workflows=[SandboxExecutionWorkflow]).replay_workflow(history)
    assert calls == previous
