"""Trusted metadata and control-plane tests. No user code executes on the host."""

from copy import deepcopy
import hashlib
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.verification import ContractInput
from app.db.models import (Evaluation, Goal, Operation, Task, TestContract as Contract,
                           VerificationCase as Case, VerificationRun as Run)
from app.domain import commands, verification as domain
from app.domain.common import DomainError
from app.integrations.settings import runtime_settings
from app.integrations.verification import VerificationTransport
from app.integrations.workspace_proto import workspace_pb2 as pb
from app.main import app


@pytest.fixture
def enabled(monkeypatch, database):
    monkeypatch.setattr(runtime_settings(), "verification_enabled", True)
    monkeypatch.setattr(domain, "SessionFactory", database[0])


async def saved_case(database, *, samples=None, code="print(input())", comparator="exact"):
    from test_domain import active_goal
    factory, user, _ = database
    goal, task, _ = await active_goal(user)
    submission = await commands.submit(user.id, task["id"], {"text": code, "attachment_ids": [], "expected_version": task["state_version"],
        "attempt_id": str(uuid4()), "minutes": 1, "estimated": False}, str(uuid4()))
    async with factory() as db:
        current = await db.get(Task, task["id"])
        task_version, requirement = current.state_version, current.requirements_version
    payload = {"expected_version": task_version, "expected_goal_revision": goal["control_revision"],
        "requirements_version": requirement, "confirm": True, "comparator": comparator,
        "cases": samples or [{"name": "回显", "stdin": "hello\n", "expected_stdout": "hello\n"}]}
    contract = await domain.confirm_contract(user.id, task["id"], payload, str(uuid4()))
    request = {"expected_version": task_version, "expected_goal_revision": goal["control_revision"],
        "contract_id": contract["id"], "expected_contract_hash": contract["contract_hash"], "attachment_id": None}
    run = await domain.create_run(user.id, submission["resource_id"], request, str(uuid4()))
    return run, contract, payload, request


class Node:
    """Test double for the trusted RPC service, not an emulation of Python execution."""

    def __init__(self, factory, responses=None):
        self.factory, self.responses = factory, responses or [{}]
        self.calls, self.writes, self.current = [], [], {}
        self.cleanup_pending = 0
        self.crash_open = self.crash_start = False

    def client(self, operation_id, case_id):
        node = self
        class Client:
            async def scope(self, action, cleanup=False):
                scope = await domain.transport_scope(operation_id, case_id, action=action, cleanup=cleanup)
                node.calls.append((action, scope["workspace_id"]))
                return scope

            async def open(self):
                scope = await self.scope("Open")
                node.current[scope["workspace_id"]] = True
                if node.crash_open:
                    node.crash_open = False
                    raise RuntimeError("simulated activity crash after delivery")
                return pb.WorkspaceStatus(workspace_id=scope["workspace_id"], state="open", working_dir="/workspace")

            async def status(self):
                scope = await self.scope("Status", True)
                return pb.WorkspaceStatus(workspace_id=scope["workspace_id"], state="open" if node.current.get(scope["workspace_id"]) else "closed",
                                          working_dir="/workspace", cleanup_done=not node.current.get(scope["workspace_id"], False))

            async def write(self, path, data):
                scope = await self.scope("Filesystem")
                assert hashlib.sha256(data).hexdigest() == scope["file_hashes"][path]
                node.writes.append((scope["workspace_id"], path, data))

            async def receipt(self, action):
                scope = await self.scope(action, action != "StartCommand")
                async with node.factory() as db:
                    case = await db.get(Case, case_id)
                    response = deepcopy(node.responses[min(case.index - 1, len(node.responses) - 1)])
                remote = pb.CommandReply(command_id=scope["command_id"], state=response.pop("state", "finished"),
                    stdout=response.pop("stdout", b"hello\n"), stderr=response.pop("stderr", b""),
                    output_complete=response.pop("output_complete", True), exit_code_available=response.pop("exit_code_available", True),
                    exit_code=response.pop("exit_code", 0), **response)
                if action == "StartCommand" and node.crash_start:
                    node.crash_start = False
                    raise RuntimeError("simulated activity crash after command")
                return remote

            async def start(self):
                return await self.receipt("StartCommand")

            async def poll(self):
                return await self.receipt("PollCommand")

            async def cancel(self):
                remote = await self.receipt("CancelCommand")
                remote.cancelled, remote.state = True, "finished"
                return remote

            async def close(self):
                scope = await self.scope("Close", True)
                async with node.factory() as db:
                    case = await db.get(Case, case_id)
                    assert case.result_saved  # Persist any definitive/inconclusive record before destroying output.
                if node.cleanup_pending:
                    node.cleanup_pending -= 1
                    return pb.WorkspaceStatus(workspace_id=scope["workspace_id"], state="closing", cleanup_done=False)
                node.current[scope["workspace_id"]] = False
                return pb.WorkspaceStatus(workspace_id=scope["workspace_id"], state="closed", cleanup_done=True)
        return Client()


async def drain(operation_id):
    state = await domain.prepare_verification(operation_id)
    for _ in range(70):
        if state["terminal"]:
            return state
        state = await domain.advance_verification(operation_id)
    raise AssertionError("Verification failed to reach a terminal receipt")


@pytest.mark.parametrize("response,status,reason,verified", [
    ({}, "passed", "matched", True),
    ({"stdout": b"passed\n"}, "failed", "output_mismatch", True),
    ({"stdout": b"hello\n", "exit_code": 7}, "failed", "nonzero_exit", True),
    ({"stdout": b"\xff"}, "inconclusive", "invalid_utf8", False),
    ({"output_truncated": True}, "inconclusive", "output_truncated", False),
    ({"output_complete": False}, "inconclusive", "output_incomplete", False),
    ({"exit_code_available": False}, "inconclusive", "missing_exit_code", False),
    ({"timed_out": True, "exit_code_available": False}, "failed", "timeout", False),
    ({"error_code": "INTERNAL"}, "inconclusive", "protocol_error", False),
])
async def test_only_complete_trusted_output_and_exit_can_pass(database, enabled, monkeypatch, response, status, reason, verified):
    run, _, _, _ = await saved_case(database)
    node = Node(database[0], [response])
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    state = await drain(run["operation_id"])
    public = await domain.get_run(database[1].id, run["id"])
    assert state["terminal"] and public["status"] == status and public["tests_verified"] is verified
    assert public["cases"][0]["reason"] == reason and public["cleanup_done"]
    assert public["evidence_type"] == "program_verified" and "source" not in public
    async with database[0]() as db:
        assert await db.scalar(select(func.count()).select_from(Evaluation)) == 0
        op = await db.get(Operation, run["operation_id"])
        assert op.model_requests == 0
        assert (await db.get(Task, run["task_id"])).status == "awaiting_review"


@pytest.mark.parametrize("comparator,stdout,status", [("exact", b"hello\r\n", "failed"),
    ("normalize_newlines", b"hello\r\n", "passed"), ("normalize_newlines", b"hello", "failed"),
    ("normalize_newlines", b"hello \n", "failed")])
async def test_comparison_never_trims_whitespace_or_final_newline(database, enabled, monkeypatch, comparator, stdout, status):
    run, _, _, _ = await saved_case(database, comparator=comparator)
    node = Node(database[0], [{"stdout": stdout}])
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    assert (await drain(run["operation_id"]))["status"] == status


async def test_uncertain_open_and_start_reconcile_without_reexecution_and_cleanup_does_not_rerun(database, enabled, monkeypatch):
    run, _, _, _ = await saved_case(database)
    node = Node(database[0])
    node.crash_open = node.crash_start = True
    node.cleanup_pending = 2
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    await domain.prepare_verification(run["operation_id"])
    with pytest.raises(RuntimeError):
        await domain.advance_verification(run["operation_id"])
    await domain.advance_verification(run["operation_id"])  # Status, not Open.
    await domain.advance_verification(run["operation_id"])  # Seed only before the command.
    with pytest.raises(RuntimeError):
        await domain.advance_verification(run["operation_id"])
    state = await drain(run["operation_id"])
    assert state["terminal"] and state["status"] == "passed"
    assert [name for name, _ in node.calls].count("Open") == [name for name, _ in node.calls].count("StartCommand") == 1
    before = list(node.calls)
    assert (await domain.advance_verification(run["operation_id"]))["terminal"]
    assert node.calls == before


async def test_each_case_has_fresh_workspace_and_expected_output_never_leaves_database(database, enabled, monkeypatch):
    samples = [{"name": "第一例", "stdin": "one", "expected_stdout": "private-expected-one"},
               {"name": "第二例", "stdin": "two", "expected_stdout": "private-expected-two"}]
    run, _, _, _ = await saved_case(database, samples=samples, code="print('a program, not a test runner')")
    node = Node(database[0], [{"stdout": b"private-expected-one"}, {"stdout": b"private-expected-two"}])
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    assert (await drain(run["operation_id"]))["status"] == "passed"
    workspaces = {workspace for name, workspace in node.calls if name == "Open"}
    assert len(workspaces) == 2
    sources = [data for _, path, data in node.writes if path.endswith("source.py")]
    assert len(sources) == 2 and sources[0] == sources[1]
    assert all(b"private-expected" not in data for _, _, data in node.writes)


async def test_scope_revocation_cancels_running_command_and_pending_cases(database, enabled, monkeypatch):
    samples = [{"name": str(index), "stdin": "", "expected_stdout": ""} for index in range(2)]
    run, _, _, _ = await saved_case(database, samples=samples)
    node = Node(database[0], [{"state": "running", "output_complete": False, "exit_code_available": False}])
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    await domain.prepare_verification(run["operation_id"])
    for _ in range(3):
        await domain.advance_verification(run["operation_id"])
    async with database[0]() as db, db.begin():
        (await db.get(Goal, run["goal_id"])).control_revision += 1
    assert (await drain(run["operation_id"]))["status"] == "superseded"
    names = [name for name, _ in node.calls]
    assert names.count("Open") == 1 and "CancelCommand" in names
    assert not (await domain.get_run(database[1].id, run["id"]))["tests_verified"]


async def test_contract_confirmation_is_immutable_idempotent_and_stale_or_unowned_rules_are_rejected(database, enabled):
    factory, user, other = database
    run, contract, payload, request = await saved_case(database)
    confirmed = await domain.confirm_contract(user.id, run["task_id"], payload, "confirmed-once")
    assert await domain.confirm_contract(user.id, run["task_id"], payload, "confirmed-once") == confirmed
    assert confirmed["version"] == contract["version"] + 1
    with pytest.raises(DomainError) as conflict:
        await domain.confirm_contract(user.id, run["task_id"], {**payload, "comparator": "normalize_newlines"}, "confirmed-once")
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(DomainError):
        await domain.create_run(user.id, run["submission_id"], request, "old-contract-now")
    with pytest.raises(DomainError) as denied:
        await domain.list_contracts(other.id, run["task_id"])
    assert denied.value.status == 404
    async with factory() as db:
        original = await db.get(Contract, contract["id"])
        assert original.contract_hash == contract["contract_hash"] and original.cases == contract["cases"]


@pytest.mark.parametrize("confirm", [False, 1, "true", None])
def test_confirmation_must_be_explicit_boolean_true(confirm):
    with pytest.raises(ValidationError):
        ContractInput(expected_version=1, expected_goal_revision=1, requirements_version=1, confirm=confirm,
                      comparator="exact", cases=[{"name": "example", "stdin": "", "expected_stdout": ""}])


async def test_transport_rejects_missing_or_cross_case_scope_before_network(database, enabled):
    run, _, _, _ = await saved_case(database)
    with pytest.raises(ValueError):
        VerificationTransport(None, None)
    async with database[0]() as db:
        case = await db.scalar(select(Case).where(Case.run_id == run["id"]))
    # Even a real owner cannot bypass the durable Open intent with this transport.
    with pytest.raises(DomainError):
        await VerificationTransport(run["operation_id"], case.id).open()
    with pytest.raises(DomainError):
        await VerificationTransport(run["operation_id"], str(uuid4())).close()


async def test_cleanup_unknown_blocks_new_runs_and_preserves_trusted_result(database, enabled, monkeypatch):
    run, _, _, request = await saved_case(database)
    node = Node(database[0])
    node.cleanup_pending = 2
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    await domain.prepare_verification(run["operation_id"])
    for _ in range(4):
        state = await domain.advance_verification(run["operation_id"])
    assert state["status"] == "passed" and not state["cleanup_done"]
    with pytest.raises(DomainError) as busy:
        await domain.create_run(database[1].id, run["submission_id"], request, "second-run-key")
    assert busy.value.code == "verification_busy"
    await domain.request_cancel(database[1].id, run["id"], "retry-cleanup-one")
    await domain.request_cancel(database[1].id, run["id"], "retry-cleanup-two")
    assert (await drain(run["operation_id"]))["status"] == "passed"
    assert [name for name, _ in node.calls].count("StartCommand") == 1


async def test_evidence_is_bounded_metadata_and_invalidated_by_current_scope(database, enabled, monkeypatch):
    run, _, _, _ = await saved_case(database)
    node = Node(database[0])
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    await drain(run["operation_id"])
    async with database[0]() as db, db.begin():
        evidence = await domain.applicable_evidence(db, database[1].id, run["goal_id"])
        assert len(evidence) == 1 and evidence[0]["passed"] and evidence[0]["case_count"] == 1
        assert "source" not in evidence[0] and "stdout" not in evidence[0]
        (await db.get(Task, run["task_id"])).requirements_version += 1
        await db.flush()
        assert await domain.applicable_evidence(db, database[1].id, run["goal_id"]) == []


async def test_rest_is_authenticated_csrf_protected_and_cannot_set_passed(database, enabled):
    run, _, payload, _ = await saved_case(database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        path = f"/api/v1/verification-runs/{run['id']}"
        assert (await client.get(path)).status_code == 401
        login = await client.post("/api/v1/auth/login", json={"username": "learner", "password": "test-only-long-password"},
            headers={"Origin": "http://localhost:8080"})
        headers = {"Origin": "http://localhost:8080", "X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": "contract-http-once"}
        contract_path = f"/api/v1/tasks/{run['task_id']}/test-contracts"
        assert (await client.post(contract_path, json=payload)).status_code == 403
        assert (await client.post(contract_path, json={**payload, "passed": True}, headers=headers)).status_code == 422
        saved = await client.post(contract_path, json=payload, headers=headers)
        assert saved.status_code == 201
        assert (await client.get(path)).json()["tests_verified"] is False


async def test_rolling_quota_counts_five_runs_and_releases_only_outside_24_hours(database, enabled, monkeypatch):
    from app.db.models import now
    run, _, _, request = await saved_case(database)
    node = Node(database[0])
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    await drain(run["operation_id"])
    for _ in range(4):
        next_run = await domain.create_run(database[1].id, run["submission_id"], request, str(uuid4()))
        await drain(next_run["operation_id"])
    with pytest.raises(DomainError) as capped:
        await domain.create_run(database[1].id, run["submission_id"], request, "sixth-run-should-deny")
    assert capped.value.code == "verification_budget_exceeded"
    async with database[0]() as db, db.begin():
        (await db.get(Run, run["id"])).created_at = now() - timedelta(hours=25)
    assert (await domain.create_run(database[1].id, run["submission_id"], request, "outside-rolling-day"))["status"] == "accepted"


async def test_source_hash_corruption_prevents_any_open(database, enabled, monkeypatch):
    run, _, _, _ = await saved_case(database)
    async with database[0]() as db, db.begin():
        (await db.get(Run, run["id"])).source = b"print('not the approved saved source')"
    node = Node(database[0])
    monkeypatch.setattr(domain, "VerificationTransport", node.client)
    assert (await drain(run["operation_id"]))["status"] == "superseded"
    assert not node.calls


async def test_actual_transport_preserves_raw_output_and_fixed_command_without_expected_data(database, enabled, monkeypatch):
    from app.integrations import verification as integration
    from app.integrations.verification import COMMAND, VerificationServiceError
    run, _, _, _ = await saved_case(database)
    async with database[0]() as db, db.begin():
        case = await db.scalar(select(Case).where(Case.run_id == run["id"]))
        case.open_attempted = case.workspace_created = case.seed_saved = case.command_attempted = True
        source = (await db.get(Run, run["id"])).source
        case_id, workspace_id, command_id = case.id, case.workspace_id, case.command_id
    requests = []
    async def call(request, **kwargs):
        requests.append(request)
        return pb.CommandReply(command_id=command_id, state="finished", stdout=b"\xffraw", stderr=b"split-stderr",
                               exit_code_available=True, exit_code=7, output_complete=True)
    class Channel:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    monkeypatch.setattr(integration.grpc.aio, "insecure_channel", lambda *args, **kwargs: Channel())
    monkeypatch.setattr(integration.rpc, "WorkspaceServiceStub", lambda _: SimpleNamespace(StartCommand=call, PollCommand=call))
    transport = VerificationTransport(run["operation_id"], case_id)
    result = await transport.start()
    assert result.stdout == b"\xffraw" and result.stderr == b"split-stderr" and result.exit_code == 7
    request = requests[0]
    assert request.workspace_id == workspace_id and request.request_id == command_id and request.shell_command == COMMAND
    assert request.timeout_seconds == 10 and request.output_limit_bytes == 65536 and not request.env
    assert source not in request.SerializeToString() and b"expected_stdout" not in request.SerializeToString()
    async def wrong(request, **kwargs):
        return pb.CommandReply(command_id="different-command", state="finished", exit_code_available=True, output_complete=True)
    monkeypatch.setattr(integration.rpc, "WorkspaceServiceStub", lambda _: SimpleNamespace(PollCommand=wrong))
    with pytest.raises(VerificationServiceError):
        await transport.poll()
