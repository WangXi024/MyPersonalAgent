"""Real Temporal lifecycle/replay checks. The Agent body is a synthetic activity here."""

import asyncio
import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("TEMPORAL_TEST_ADDRESS"), reason="Set TEMPORAL_TEST_ADDRESS")


@pytest.mark.parametrize("scenario", ["success", "cancel_before", "cancel_running", "timeout", "agent_failure",
                                       "cleanup_retry", "cleanup_only"])
async def test_coding_lifecycle_cancel_deadline_export_and_replay(monkeypatch, scenario):
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from temporalio import activity, workflow
    from temporalio.client import Client
    from temporalio.exceptions import ApplicationError
    from temporalio.worker import Replayer, Worker

    from app.agents.coder import CODER_AGENT
    from app.domain import coding_runs
    from app.workflows.coding import CodingWorkflow
    from app.workflows.coding_activities import CODING_ACTIVITIES

    calls = []
    started = asyncio.Event()
    state = {"operation_id": "synthetic-coder", "run_id": "synthetic-run", "terminal": False,
             "status": "running", "stage": "agent", "workspace_created": True, "cleanup_done": False,
             "export_saved": False, "cancel_requested": scenario == "cancel_before",
             "agent_ready": scenario != "cleanup_only", "remaining_seconds": .1 if scenario == "timeout" else 60,
             "prompt": "Synthetic instruction", "deps": {}}
    if scenario == "cleanup_only":
        state.update(status="succeeded", export_saved=True)

    async def prepare(_):
        calls.append("prepare")
        return dict(state)

    async def finish(_, summary="", *, error_code=None):
        calls.append("finish")
        state.update(export_saved=True)
        if scenario == "cleanup_retry" and calls.count("finish") <= 3:
            raise ApplicationError("Synthetic Close acknowledgement loss")
        state.update(terminal=True, cleanup_done=True, stage="finished",
                     status="cancelled" if state["cancel_requested"] else "failed" if error_code else "succeeded")
        assert summary == ("Synthetic output" if scenario in {"success", "cleanup_retry"} else "")
        if scenario in {"timeout", "agent_failure"}:
            assert error_code == ("operation_timeout" if scenario == "timeout" else "agent_failed")
        return dict(state)

    async def cancel(_):
        calls.append("cancel")
        state.update(cancel_requested=True)
        return await finish(_)

    @activity.defn(name="synthetic_coding_model")
    async def synthetic_model() -> str:
        calls.append("model")
        started.set()
        if scenario == "agent_failure":
            raise ApplicationError("Synthetic model failure", non_retryable=True)
        if scenario in {"cancel_running", "timeout"}:
            await asyncio.sleep(120)
        return "Synthetic output"

    async def run(*args, **kwargs):
        result = await workflow.execute_activity(synthetic_model, start_to_close_timeout=timedelta(seconds=150))
        return SimpleNamespace(output=result)

    monkeypatch.setattr(coding_runs, "prepare_coding", prepare)
    monkeypatch.setattr(coding_runs, "finish_coding", finish)
    monkeypatch.setattr(coding_runs, "cancel_coding", cancel)
    monkeypatch.setattr(CODER_AGENT, "run", run)
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"), plugins=[PydanticAIPlugin()])
    identity = f"test-coding-{uuid4()}"
    async with Worker(client, task_queue=identity, workflows=[CodingWorkflow],
                      activities=CODING_ACTIVITIES + [synthetic_model]):
        handle = await client.start_workflow(CodingWorkflow.run, "synthetic-coder", id=identity,
            task_queue=identity, execution_timeout=timedelta(minutes=3))
        if scenario == "cancel_running":
            await asyncio.wait_for(started.wait(), 15)
            await handle.signal(CodingWorkflow.cancel)
        result = await handle.result()
    assert result["terminal"] and result["cleanup_done"] and result["export_saved"]
    assert calls.count("model") == (0 if scenario in {"cancel_before", "cleanup_only"} else 1)
    assert calls.count("prepare") == 1
    assert calls.count("finish") == (4 if scenario == "cleanup_retry" else 1)
    if scenario.startswith("cancel"):
        assert result["status"] == "cancelled" and calls.count("cancel") == 1
    previous = list(calls)
    history = await handle.fetch_history()
    await Replayer(workflows=[CodingWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
    assert calls == previous
