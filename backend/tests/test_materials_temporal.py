"""Real Temporal with synthetic RPC documents; no production node/model is contacted."""

import asyncio
from datetime import timedelta
import json
import os
from uuid import uuid4

import pytest
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from app.domain import materials as domain
from app.integrations.settings import RuntimeSettings
from app.workflows.material_activities import MATERIAL_ACTIVITIES
from app.workflows.materials import MaterialParseWorkflow
from test_materials import FakeSandbox, create


pytestmark = pytest.mark.skipif(not os.getenv("TEMPORAL_TEST_ADDRESS"), reason="Set TEMPORAL_TEST_ADDRESS")


@pytest.fixture
def config(database, monkeypatch, tmp_path):
    from types import SimpleNamespace
    settings = RuntimeSettings(material_enabled=True)
    monkeypatch.setattr(domain, "SessionFactory", database[0])
    monkeypatch.setattr(domain, "runtime_settings", lambda: settings)
    monkeypatch.setattr(domain, "get_settings", lambda: SimpleNamespace(attachment_dir=tmp_path / "files"))
    return settings


@pytest.mark.parametrize("cancel", [False, True])
async def test_material_temporal_receipts_cancel_and_replay_keep_document_out_of_history(database, config, monkeypatch, cancel):
    submitted = asyncio.Event()
    class RPC(FakeSandbox):
        async def submit(self, request):
            result = await super().submit(request)
            submitted.set()
            return result
    fake = RPC(text="PRIVATE_SYNTHETIC_DOCUMENT_BODY")
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    item = await create(database)
    client = await Client.connect(os.environ["TEMPORAL_TEST_ADDRESS"],
        namespace=os.getenv("TEMPORAL_TEST_NAMESPACE", "learning-agent"))
    identity = "material-integration-" + uuid4().hex
    async with Worker(client, task_queue=identity, workflows=[MaterialParseWorkflow], activities=MATERIAL_ACTIVITIES):
        handle = await client.start_workflow(MaterialParseWorkflow.run, item["id"], id=identity,
            task_queue=identity, execution_timeout=timedelta(minutes=6))
        await asyncio.wait_for(submitted.wait(), timeout=30)
        if cancel:
            await domain.request_cancel(database[1].id, item["id"], str(uuid4()))
            await handle.signal(MaterialParseWorkflow.cancel)
        state = await handle.result()
    assert state["terminal"] and state["cleanup_done"] and state["delivery_done"]
    assert state["status"] == ("cancelled" if cancel else "succeeded")
    assert fake.calls.count("submit") == fake.calls.count("result") == fake.calls.count("ack") == 1
    history = await handle.fetch_history()
    assert "PRIVATE_SYNTHETIC_DOCUMENT_BODY" not in history.to_json()
    before = list(fake.calls)
    await Replayer(workflows=[MaterialParseWorkflow]).replay_workflow(history)
    assert fake.calls == before
    public = await domain.get_material(database[1].id, item["id"])
    assert "PRIVATE_SYNTHETIC_DOCUMENT_BODY" not in json.dumps(public)
