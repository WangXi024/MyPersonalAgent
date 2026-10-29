"""Dedicated real Temporal orchestration/replay, using only a fake RPC service."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.models import VerificationCase
from app.domain import verification
from app.integrations.settings import runtime_settings
from app.workflows.verification import VerificationWorkflow
from app.workflows.verification_activities import VERIFICATION_ACTIVITIES
from test_verification import Node, saved_case

pytestmark = pytest.mark.skipif(not os.getenv("TEMPORAL_TEST_ADDRESS"), reason="Real Temporal server required")


@pytest.fixture
def enabled(monkeypatch, database):
    monkeypatch.setattr(runtime_settings(), "verification_enabled", True)
    monkeypatch.setattr(verification, "SessionFactory", database[0])


@pytest.mark.parametrize("cancel", [False, True])
async def test_verification_lifecycle_has_no_source_payload_and_replay_never_reruns(database, enabled, monkeypatch, cancel):
    from temporalio.client import Client
    from temporalio.worker import Replayer, Worker
    run, _, _, _ = await saved_case(database, code="print('PRIVATE_SOURCE_NEVER_HISTORY')",
        samples=[{"name": "样例", "stdin": "private-input", "expected_stdout": "PRIVATE_EXPECTED_NEVER_HISTORY"}])
    node = Node(database[0], [{"state": "running", "output_complete": False, "exit_code_available": False}] if cancel
                else [{"stdout": b"PRIVATE_EXPECTED_NEVER_HISTORY"}])
    monkeypatch.setattr(verification, "VerificationTransport", node.client)
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"], namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"))
    identity = f"verification-test-{uuid4()}"
    async with Worker(client, task_queue=identity, workflows=[VerificationWorkflow], activities=VERIFICATION_ACTIVITIES):
        handle = await client.start_workflow(VerificationWorkflow.run, run["operation_id"], id=identity,
                                            task_queue=identity, execution_timeout=timedelta(minutes=2))
        if cancel:
            for _ in range(120):
                async with database[0]() as db:
                    started = await db.scalar(select(VerificationCase.command_attempted).where(VerificationCase.run_id == run["id"]))
                if started:
                    break
                await asyncio.sleep(0.1)
            assert started
            await handle.signal(VerificationWorkflow.cancel)
        result = await handle.result()
    assert result["terminal"] and result["cleanup_done"] and result["status"] == ("cancelled" if cancel else "passed")
    calls = list(node.calls)
    history = await handle.fetch_history()
    serialized = b"".join(event.SerializeToString() for event in history.events)
    assert b"PRIVATE_SOURCE_NEVER_HISTORY" not in serialized and b"PRIVATE_EXPECTED_NEVER_HISTORY" not in serialized
    await Replayer(workflows=[VerificationWorkflow]).replay_workflow(history)
    assert node.calls == calls
