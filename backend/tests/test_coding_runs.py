"""Control-plane tests use mocks; they make no claims about a real sandbox passing POSIX tests."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.api import coding as api, routes
from app.db.models import CodingArtifact, CodingCommand, CodingRun, Evaluation, Operation, Outbox, Task, User, now
from app.domain import coding_runs as domain
from app.domain.common import DomainError
from app.integrations import workspace
from app.integrations.settings import RuntimeSettings
from app.main import app
from test_sandbox_execution import saved_submission


@pytest.fixture
def config(monkeypatch, tmp_path):
    settings = RuntimeSettings(coding_enabled=True)
    monkeypatch.setattr(domain, "runtime_settings", lambda: settings)
    from app.domain import sandbox_execution
    monkeypatch.setattr(sandbox_execution, "get_settings", lambda: SimpleNamespace(attachment_dir=tmp_path / "files"))
    return settings


async def create(database):
    submission_id, original, task_id = await saved_submission(database)
    payload = {key: original[key] for key in ("expected_version", "expected_goal_revision", "attachment_id")}
    payload["instruction"] = "请解释这段代码，并补充一个标准库测试"
    item = await domain.create_run(database[1].id, submission_id, payload, str(uuid4()))
    return item, payload, task_id


@pytest.fixture
def remote(monkeypatch):
    state = SimpleNamespace(open_count=0, close_count=0, writes=[], files=[("submission.py", b"print('fixed')")],
                            export_error=False, close_error=False)

    class Backend:
        def __init__(self, *, operation_id, workspace_id, **kwargs):
            self.operation_id, self.workspace_id = operation_id, workspace_id

        async def working_dir(self):
            state.open_count += 1
            await domain.workspace_opened(self.operation_id, self.workspace_id)
            return "/workspace"

        async def write_bytes(self, path, data):
            state.writes.append((path, data))

        async def export_files(self):
            if state.export_error:
                raise OSError("unavailable")
            return state.files

        async def close(self):
            state.close_count += 1
            if state.close_error:
                raise OSError("unavailable")

    monkeypatch.setattr(workspace, "SandboxWorkspaceBackend", Backend)
    return state


async def test_saved_scope_seed_receipts_export_cleanup_never_modify_learning(database, config, remote):
    item, payload, task_id = await create(database)
    assert item["status"] == "accepted" and not item["workspace_created"]
    assert "source" not in item and "workspace_id" not in item
    op_id = item["operation_id"]
    state = await domain.prepare_coding(op_id)
    assert state["agent_ready"] and state["deps"]["operation"]["id"] == op_id
    assert "print" not in state["prompt"]  # source travels directly to RPC, not Workflow input.
    assert remote.writes == [("/workspace/submission.py", "print('通过')".encode())]
    assert (await domain.prepare_coding(op_id))["agent_ready"]
    assert len(remote.writes) == 1
    await domain.record_command(op_id, "cmd1", "python submission.py", {
        "state": "finished", "exit_code": 7, "stdout": "通过", "stderr": "real error"})
    final = await domain.finish_coding(op_id, "建议已保存，实际退出码7")
    assert final["terminal"] and final["cleanup_done"] and final["export_saved"]
    assert (await domain.prepare_coding(op_id))["agent_ready"] is False
    await domain.finish_coding(op_id, "different replay")
    assert remote.close_count == 1
    factory, user, _ = database
    async with factory() as db:
        row = await db.get(CodingRun, item["id"])
        public = await domain.public_run(db, row)
        assert public["commands"][0]["exit_code"] == 7
        assert public["commands"][0]["stdout"] == "通过"
        assert public["summary"] == "建议已保存，实际退出码7"
        artifact = await db.scalar(select(CodingArtifact).where(CodingArtifact.run_id == row.id))
        assert domain.storage_path(artifact.storage_key).read_bytes() == b"print('fixed')"
        assert await db.scalar(select(func.count()).select_from(Evaluation)) == 0
        task = await db.get(Task, task_id)
        assert task.active_submission_id == item["submission_id"]
        outbox = await db.scalar(select(Outbox).where(Outbox.operation_id == op_id))
        assert outbox.payload["type"] == "coding_run"


async def test_idempotency_and_explicit_model_snapshot(database, config):
    item, payload, _ = await create(database)
    factory, user, _ = database
    async with factory() as db:
        op = await db.get(Operation, item["operation_id"])
        key = op.idempotency_key
        assert op.versions["model_alias"] and op.usage_details
    repeated = await domain.create_run(user.id, item["submission_id"], payload, key)
    assert repeated["id"] == item["id"]
    with pytest.raises(DomainError, match="幂等"):
        await domain.create_run(user.id, item["submission_id"], {**payload, "instruction": "other"}, key)


async def test_cancel_before_open_is_idempotent_no_remote_io(database, config, remote):
    item, _, _ = await create(database)
    user = database[1]
    for _ in range(2):
        await domain.request_cancel(user.id, item["id"], str(uuid4()))
    state = await domain.prepare_coding(item["operation_id"])
    assert state["cancel_requested"] and not state["agent_ready"]
    final = await domain.cancel_coding(item["operation_id"])
    assert final["status"] == "cancelled" and final["terminal"]
    assert remote.open_count == remote.close_count == 0
    async with database[0]() as db:
        count = await db.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.dedupe_key == f"coding-cancel:{item['id']}"))
        assert count == 1


@pytest.mark.parametrize("changed", ["submission", "user", "deadline", "operation"])
async def test_workspace_and_model_fences(database, config, remote, changed):
    item, _, task_id = await create(database)
    await domain.prepare_coding(item["operation_id"])
    factory, user, _ = database
    async with factory() as db, db.begin():
        row = await db.get(CodingRun, item["id"])
        if changed == "submission":
            (await db.get(Task, task_id)).active_submission_id = None
        elif changed == "user":
            (await db.get(User, user.id)).active = False
        elif changed == "deadline":
            row.created_at = now() - timedelta(hours=1)
        else:
            (await db.get(Operation, item["operation_id"])).status = "cancelled"
    assert (await domain.workspace_scope(item["operation_id"]))["allowed"] is False
    result = await domain.cancel_coding(item["operation_id"])
    assert result["cleanup_done"] and remote.close_count == 1


async def test_cleanup_retry_does_not_reseed_rerun_or_lose_summary(database, config, remote):
    item, _, _ = await create(database)
    await domain.prepare_coding(item["operation_id"])
    remote.close_error = True
    with pytest.raises(OSError):
        await domain.finish_coding(item["operation_id"], "已完成修改")
    state = await domain.prepare_coding(item["operation_id"])
    assert not state["terminal"] and not state["agent_ready"] and state["export_saved"]
    assert not state["cleanup_done"]
    remote.close_error = False
    result = await domain.finish_coding(item["operation_id"])
    assert result["terminal"] and len(remote.writes) == 1
    async with database[0]() as db:
        assert (await db.get(CodingRun, item["id"])).summary == "已完成修改"


async def test_completed_outcome_survives_deadline_context_edit_and_cleanup_cancel(database, config, remote):
    item, _, task_id = await create(database)
    await domain.prepare_coding(item["operation_id"])
    remote.close_error = True
    with pytest.raises(OSError):
        await domain.finish_coding(item["operation_id"], "编码已完成")
    async with database[0]() as db, db.begin():
        row = await db.get(CodingRun, item["id"])
        row.created_at = now() - timedelta(hours=1)
        (await db.get(Task, task_id)).active_submission_id = None
    await domain.request_cancel(database[1].id, item["id"], str(uuid4()))
    remote.close_error = False
    state = await domain.cancel_coding(item["operation_id"])
    assert state["status"] == "succeeded" and state["terminal"]


async def test_export_failure_still_closes_and_reports_missing_export(database, config, remote):
    item, _, _ = await create(database)
    await domain.prepare_coding(item["operation_id"])
    remote.export_error = True
    state = await domain.finish_coding(item["operation_id"], "finished")
    assert state["cleanup_done"] and state["status"] == "failed" and remote.close_count == 1
    async with database[0]() as db:
        assert "无法导出" in (await db.get(CodingRun, item["id"])).error_message


async def test_open_response_lost_still_closes_stable_identity(database, config, remote):
    item, _, _ = await create(database)
    async with database[0]() as db, db.begin():
        row = await db.get(CodingRun, item["id"])
        row.workspace_open_attempted = True
        row.status, row.stage = "running", "opening"
    assert not (await domain.workspace_scope(item["operation_id"]))["workspace_created"]
    result = await domain.fail_coding(item["operation_id"])
    assert result["cleanup_done"] and result["terminal"] and remote.close_count == 1


async def test_storage_failure_cannot_leave_workspace_alive(database, config, remote, monkeypatch):
    item, _, _ = await create(database)
    await domain.prepare_coding(item["operation_id"])
    def unavailable(*_):
        raise OSError("disk unavailable")
    monkeypatch.setattr(domain, "_save_file", unavailable)
    state = await domain.finish_coding(item["operation_id"], "finished")
    assert state["cleanup_done"] and state["status"] == "failed" and remote.close_count == 1
    async with database[0]() as db:
        assert await db.scalar(select(func.count()).select_from(CodingArtifact)) == 0


async def test_api_scope_limits_auth_and_goal_detail(database, config):
    item, payload, _ = await create(database)
    factory, user, other = database
    assert (await routes.goal_detail(item["goal_id"], user))["coding_runs"][0]["id"] == item["id"]
    with pytest.raises(DomainError):
        await api.get_run(item["id"], other)
    with pytest.raises(DomainError) as error:
        await domain.create_run(user.id, item["submission_id"], payload, str(uuid4()))
    assert error.value.code == "coding_busy"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        response = await client.post(f"/api/v1/submissions/{item['submission_id']}/coding-runs", json=payload,
                                     headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 401


async def test_record_command_is_stable_and_unknown_exit_code_is_not_zero(database, config):
    item, _, _ = await create(database)
    for status in ("running", "finished"):
        await domain.record_command(item["operation_id"], "same", "python", {
            "state": status, "exit_code": None, "stdout": "passed", "stderr": ""})
    await domain.record_command(item["operation_id"], "same", "python", {
        "state": "running", "exit_code": None, "stdout": "", "stderr": ""})
    async with database[0]() as db:
        rows = list(await db.scalars(select(CodingCommand)))
        assert len(rows) == 1 and rows[0].exit_code is None
