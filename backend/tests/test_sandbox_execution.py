import hashlib
from datetime import timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import grpc
import httpx
import pytest
from sqlalchemy import func, select

from app.api import sandbox as api
from app.db.models import Attachment, Evaluation, Operation, Outbox, SandboxExecution, Submission, Task, User, now
from app.domain import commands, sandbox_execution as domain
from app.domain.common import DomainError
from app.integrations import sandbox
from app.integrations.sandbox_proto import sandbox_pb2 as pb, sandbox_pb2_grpc as rpc
from app.integrations.settings import RuntimeSettings
from app.main import app
from test_domain import active_goal


@pytest.fixture
def config(monkeypatch, tmp_path):
    settings = RuntimeSettings(sandbox_enabled=True)
    monkeypatch.setattr(domain, "runtime_settings", lambda: settings)
    monkeypatch.setattr(domain, "get_settings", lambda: SimpleNamespace(attachment_dir=tmp_path / "artifacts"))
    return settings


async def saved_submission(database):
    factory, user, _ = database
    goal, task, _ = await active_goal(user)
    submission = await commands.submit(user.id, task["id"], {
        "text": "print('通过')", "attachment_ids": [], "expected_version": task["state_version"]}, str(uuid4()))
    async with factory() as db:
        current = await db.get(Task, task["id"])
        payload = {"expected_version": current.state_version, "expected_goal_revision": goal["control_revision"],
                   "language": "python", "attachment_id": None}
    return submission["resource_id"], payload, task["id"]


class FakeSandbox:
    def __init__(self):
        self.request = None
        self.calls = []
        self.delivery = False
        self.outcome = "success"
        self.cleanup = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def ready(self):
        return {"ready": True, "accepting_tasks": True, "independent": True}

    def remote(self, phase="finished"):
        return sandbox.status_dict(pb.JobStatus(request_id=self.request.request_id, phase=phase,
            outcome=self.outcome if phase == "finished" else "", cleanup_done=phase == "finished" and self.cleanup,
            delivery_done=self.delivery), self.request.request_id)

    async def submit(self, request):
        self.calls.append("submit")
        self.request = request
        return self.remote("queued")

    async def status(self, request_id):
        return self.remote()

    async def result(self, request_id):
        self.calls.append("result")
        content = b"untrusted artifact"
        return sandbox.validated_result(pb.JobResult(status=pb.JobStatus(request_id=request_id,
            phase="finished", outcome=self.outcome, cleanup_done=self.cleanup), stdout="通过", artifacts=[
                pb.Artifact(path="reports/output.txt", data=content, sha256=hashlib.sha256(content).hexdigest())]),
            request_id)

    async def acknowledge(self, request_id):
        self.calls.append("ack")
        self.delivery = True
        return self.remote()

    async def cancel(self, request_id):
        self.calls.append("cancel")
        self.outcome = "cancelled"
        return self.remote()


async def test_full_execution_receipt_is_saved_before_ack_and_never_becomes_learning_evidence(database, monkeypatch, config):
    submission, payload, task_id = await saved_submission(database)
    factory, user, _ = database
    key = str(uuid4())
    item = await domain.create_execution(user.id, submission, payload, key)
    assert await domain.create_execution(user.id, submission, payload, key) == item
    assert "code" not in item and "request_hash" not in item
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    operation_id = item["operation_id"]
    state = await domain.submit_execution(operation_id)
    assert state["submitted"] and not state["terminal"]
    assert fake.request.code == b"print('\xe9\x80\x9a\xe8\xbf\x87')"
    assert not fake.request.network_domains and not fake.request.dependencies and not fake.request.environment_variables
    assert await domain.submit_execution(operation_id)  # same request reattaches instead of executing twice
    assert fake.calls == ["submit"]
    assert (await domain.poll_execution(operation_id))["phase"] == "finished"
    state = await domain.collect_execution(operation_id)
    assert state["result_saved"] and state["needs_ack"] and not state["terminal"]
    assert "ack" not in fake.calls
    async with factory() as db:
        saved = await db.get(SandboxExecution, item["id"])
        response = await domain.public_execution(db, saved)
        assert response["stdout"] == "通过" and response["status"] == "succeeded"
        assert response["artifacts"][0]["size"] == 18
        assert (await db.get(Task, task_id)).status == "awaiting_review"
        assert await db.scalar(select(func.count()).select_from(Evaluation)) == 0
    # Replayed collection and ACK remain idempotent.
    await domain.collect_execution(operation_id)
    final = await domain.ack_execution(operation_id)
    assert final["terminal"] and final["delivery_done"] and final["cleanup_done"]
    await domain.ack_execution(operation_id)
    assert fake.calls == ["submit", "result", "ack"]


async def test_disabled_stale_and_cross_user_submissions_are_rejected(database, config):
    submission, payload, _ = await saved_submission(database)
    _, user, other = database
    config.sandbox_enabled = False
    with pytest.raises(DomainError, match="尚未启用"):
        await domain.create_execution(user.id, submission, payload, str(uuid4()))
    config.sandbox_enabled = True
    with pytest.raises(DomainError, match="内容已更新"):
        await domain.create_execution(user.id, submission, {**payload, "expected_version": 999}, str(uuid4()))
    with pytest.raises(DomainError, match="不存在"):
        await domain.create_execution(other.id, submission, payload, str(uuid4()))


async def test_hard_concurrency_and_daily_budget(database, config):
    submission, payload, _ = await saved_submission(database)
    factory, user, _ = database
    config.sandbox_max_concurrent_per_user = 1
    first = await domain.create_execution(user.id, submission, payload, str(uuid4()))
    with pytest.raises(DomainError, match="已有代码任务"):
        await domain.create_execution(user.id, submission, payload, str(uuid4()))
    async with factory() as db, db.begin():
        (await db.get(SandboxExecution, first["id"])).status = "cancelled"
    config.sandbox_max_daily_executions = 1
    with pytest.raises(DomainError, match="24小时"):
        await domain.create_execution(user.id, submission, payload, str(uuid4()))


async def test_duplicate_cancellation_uses_one_outbox_and_never_starts_code(database, monkeypatch, config):
    submission, payload, _ = await saved_submission(database)
    factory, user, _ = database
    item = await domain.create_execution(user.id, submission, payload, str(uuid4()))
    await domain.request_cancel(user.id, item["id"], str(uuid4()))
    await domain.request_cancel(user.id, item["id"], str(uuid4()))
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    state = await domain.submit_execution(item["operation_id"])
    assert state["terminal"] and state["status"] == "cancelled" and not fake.calls
    async with factory() as db:
        count = await db.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.dedupe_key == f"sandbox-cancel:{item['id']}"))
        assert count == 1


@pytest.mark.parametrize("change", ["deadline", "account", "submission", "generic_cancel"])
async def test_changed_scope_blocks_new_remote_submission(database, monkeypatch, config, change):
    submission, payload, task_id = await saved_submission(database)
    factory, user, _ = database
    item = await domain.create_execution(user.id, submission, payload, str(uuid4()))
    async with factory() as db, db.begin():
        row = await db.get(SandboxExecution, item["id"])
        if change == "deadline":
            row.created_at = now() - timedelta(seconds=999)
        elif change == "account":
            (await db.get(User, user.id)).active = False
        elif change == "submission":
            (await db.get(Task, task_id)).active_submission_id = None
        else:
            (await db.get(Operation, item["operation_id"])).status = "cancelled"
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    state = await domain.submit_execution(item["operation_id"])
    assert state["terminal"] and not state["submitted"] and not fake.calls


async def test_partial_artifact_is_recovered_atomically_and_expired_body_is_not_acked(database, monkeypatch, config):
    submission, payload, _ = await saved_submission(database)
    _, user, _ = database
    item = await domain.create_execution(user.id, submission, payload, str(uuid4()))
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_execution(item["operation_id"])
    artifact_id = str(uuid5(NAMESPACE_URL, f"{item['id']}/reports/output.txt"))
    path = domain.storage_path(f"sandbox/{item['id']}/{artifact_id}")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"partial")
    (path.parent / ".pending-interrupted").write_bytes(b"partial")
    await domain.collect_execution(item["operation_id"])
    assert path.read_bytes() == b"untrusted artifact"
    assert not list(path.parent.glob(".pending-*"))
    assert "ack" not in fake.calls


async def test_expired_result_never_sends_ack(database, monkeypatch, config):
    submission, payload, _ = await saved_submission(database)
    item = await domain.create_execution(database[1].id, submission, payload, str(uuid4()))
    fake = FakeSandbox()

    async def expired(_):
        raise sandbox.SandboxRPCError("NOT_FOUND", "RESULT_EXPIRED")

    fake.result = expired
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_execution(item["operation_id"])
    state = await domain.collect_execution(item["operation_id"])
    assert state["status"] == "expired" and state["terminal"] and not state["needs_ack"]
    assert "ack" not in fake.calls


async def test_running_cancellation_collects_real_receipt_and_ack(database, monkeypatch, config):
    submission, payload, task_id = await saved_submission(database)
    item = await domain.create_execution(database[1].id, submission, payload, str(uuid4()))
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_execution(item["operation_id"])
    await domain.request_cancel(database[1].id, item["id"], str(uuid4()))
    state = await domain.cancel_execution(item["operation_id"])
    assert state["phase"] == "finished" and not state["result_saved"]
    state = await domain.collect_execution(item["operation_id"])
    assert state["status"] == "cancelled" and state["needs_ack"]
    assert (await domain.ack_execution(item["operation_id"]))["terminal"]
    async with database[0]() as db:
        assert (await db.get(Task, task_id)).status == "awaiting_review"


async def test_ack_transport_failure_preserves_saved_outcome_and_can_resume(database, monkeypatch, config):
    submission, payload, _ = await saved_submission(database)
    item = await domain.create_execution(database[1].id, submission, payload, str(uuid4()))
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_execution(item["operation_id"])
    await domain.collect_execution(item["operation_id"])
    pending = await domain.fail_execution(item["operation_id"], "service_unavailable")
    assert pending["result_saved"] and pending["status"] == "succeeded" and pending["needs_ack"]
    assert not pending["terminal"]
    final = await domain.ack_execution(item["operation_id"])
    assert final["terminal"] and final["status"] == "succeeded"
    async with database[0]() as db:
        saved = await db.get(SandboxExecution, item["id"])
        assert saved.stdout == "通过" and saved.error_code is None


async def test_unknown_receipt_keeps_polling_until_cleanup_confirmed(database, monkeypatch, config):
    submission, payload, _ = await saved_submission(database)
    item = await domain.create_execution(database[1].id, submission, payload, str(uuid4()))
    fake = FakeSandbox()
    fake.outcome, fake.cleanup = "unknown", False
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_execution(item["operation_id"])
    await domain.collect_execution(item["operation_id"])
    state = await domain.ack_execution(item["operation_id"])
    assert state["delivery_done"] and not state["cleanup_done"] and not state["terminal"]
    config.sandbox_max_concurrent_per_user = 1
    with pytest.raises(DomainError, match="已有代码任务"):
        await domain.create_execution(database[1].id, submission, payload, str(uuid4()))
    fake.cleanup = True
    assert (await domain.poll_execution(item["operation_id"]))["terminal"]


async def test_selected_saved_python_attachment_is_snapshotted(database, monkeypatch, config):
    submission, payload, _ = await saved_submission(database)
    factory, user, _ = database
    code = b"print(42)"
    path = domain.storage_path("saved-code-file")
    path.parent.mkdir(parents=True)
    path.write_bytes(code)
    async with factory() as db, db.begin():
        attachment = Attachment(user_id=user.id, filename="answer.py", storage_key="saved-code-file",
                                size=len(code), sha256=hashlib.sha256(code).hexdigest(), mime="text/plain")
        db.add(attachment)
        await db.flush()
        (await db.get(Submission, submission)).attachment_ids = [attachment.id]
    payload["attachment_id"] = attachment.id
    item = await domain.create_execution(user.id, submission, payload, str(uuid4()))
    # A later on-disk mutation cannot change the approved immutable request snapshot.
    path.write_bytes(b"print('changed')")
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_execution(item["operation_id"])
    assert fake.request.code == code and item["attachment_id"] == attachment.id


@pytest.mark.parametrize("filename", [".", "../x", "/etc/passwd", "C:/data", "a\\b", "a//b", "x\nname"])
def test_untrusted_artifact_paths_are_rejected(filename):
    body = b"x"
    result = pb.JobResult(status=pb.JobStatus(request_id="job", phase="finished", outcome="success"),
        artifacts=[pb.Artifact(path=filename, data=body, sha256=hashlib.sha256(body).hexdigest())])
    with pytest.raises(sandbox.SandboxProtocolError):
        sandbox.validated_result(result, "job")


async def test_api_ownership_strict_input_and_goal_detail(database, config):
    submission, payload, task_id = await saved_submission(database)
    _, user, other = database
    item = await api.create_execution(submission, api.ExecutionInput(**payload), user, str(uuid4()))
    assert (await api.executions(submission, user))["items"][0]["id"] == item["id"]
    with pytest.raises(DomainError):
        await api.execution(item["id"], other)
    from app.api import routes
    detail = await routes.goal_detail(item["goal_id"], user)
    assert detail["sandbox_executions"][0]["id"] == item["id"]
    assert detail["submissions"][0]["attachments"] == []
    # Ordinary browser callers still require authentication, CSRF and the strict Python DTO.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        response = await client.post(f"/api/v1/submissions/{submission}/executions", json=payload,
                                    headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 401


async def test_aio_client_uses_protocol_and_requires_independent_mode(monkeypatch, config):
    class Service(rpc.SandboxServiceServicer):
        async def Ready(self, request, context):
            return pb.ReadyReply(ready=True, accepting_tasks=True, mode="independent", environments=["python"])

        async def Submit(self, request, context):
            assert request.environment == "python" and not request.network_domains
            return pb.JobStatus(request_id=request.request_id, phase="queued")

    server = grpc.aio.server()
    rpc.add_SandboxServiceServicer_to_server(Service(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    config.sandbox_address = f"127.0.0.1:{port}"
    monkeypatch.setattr(sandbox, "runtime_settings", lambda: config)
    try:
        async with sandbox.SandboxClient() as client:
            assert (await client.ready())["independent"]
            status = await client.submit(sandbox.execution_request("job", "print(1)", {"memory_mb": 256, "timeout_seconds": 30}))
            assert status["phase"] == "queued"
    finally:
        await server.stop(0)
