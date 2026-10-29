"""RPC adapter unit tests; the separate opt-in suite exercises actual POSIX processes."""

import asyncio
from types import SimpleNamespace

import anyio
import grpc
import pytest
from pydantic_ai.workspaces import WorkspaceOutputLimitError, WorkspaceRef, WorkspaceTimeoutError

from app.integrations import workspace
from app.integrations.settings import RuntimeSettings
from app.integrations.workspace_proto import workspace_pb2 as pb, workspace_pb2_grpc as rpc


class StubbedBackend(workspace.SandboxWorkspaceBackend):
    def __init__(self):
        super().__init__(workspace_id="stub")
        self.calls = []
        self.reply = pb.CommandReply(state="finished", exit_code_available=True, exit_code=7,
                                     stdout=b"passed\xff", stderr=b"error", output_complete=True)

    async def _rpc(self, method, request, **kwargs):
        self.calls.append((method, request))
        if method in {"Open", "Status"}:
            return pb.WorkspaceStatus(workspace_id="stub", state="open", working_dir="/workspace")
        if method == "Close":
            return pb.WorkspaceStatus(workspace_id="stub", state="closed", cleanup_done=True)
        if method in {"StartCommand", "PollCommand", "CancelCommand"}:
            self.reply.command_id = request.request_id if method == "StartCommand" else request.command_id
            return self.reply
        return pb.FileReply(data=b"data")


async def test_trusted_result_logs_and_scope_are_remote_not_inferred():
    backend = StubbedBackend()
    assert backend.ref is None
    result = await backend.run(["python", "submission.py"])
    assert result.exit_code == 7 and result.stdout == "passed\ufffd" and result.stderr == "error"
    assert backend.ref == WorkspaceRef(provider=workspace.PROVIDER, id="stub")
    assert (await backend.working_dir()) == "/workspace"
    assert len([call for call in backend.calls if call[0] == "Open"]) == 1


@pytest.mark.parametrize("field", ["timed_out", "output_truncated", "output_complete", "exit_code_available"])
async def test_incomplete_output_never_returns_false_success(field):
    backend = StubbedBackend()
    setattr(backend.reply, field, field in {"timed_out", "output_truncated"})
    error = {"timed_out": WorkspaceTimeoutError, "output_truncated": WorkspaceOutputLimitError}.get(
        field, workspace.WorkspaceProtocolError)
    with pytest.raises(error):
        await backend.run("echo passed", shell=True)


async def test_cancellation_uses_control_plane_cancel_not_stdout():
    backend = StubbedBackend()
    backend.reply.state = "running"
    original = backend._rpc
    async def rpc(method, request, **kwargs):
        if method == "CancelCommand":
            backend.reply.state = "finished"
        return await original(method, request, **kwargs)
    backend._rpc = rpc
    async def command():
        await backend.run(["sleep", "60"])
    task = asyncio.create_task(command())
    await anyio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "CancelCommand" in [call[0] for call in backend.calls]


def test_stable_activity_retry_identity_and_cross_operation_ref_rejection(monkeypatch):
    monkeypatch.setattr(workspace.activity, "info", lambda: SimpleNamespace(workflow_run_id="run", activity_id="tool"))
    first, retry = StubbedBackend(), StubbedBackend()
    assert first._request_id({"code": "same"}) == retry._request_id({"code": "same"})
    assert first._request_id({"code": "same"}) != retry._request_id({"code": "different"})
    with pytest.raises(ValueError):
        workspace.SandboxWorkspaceBackend(operation_id="abc", workspace_id="other")


@pytest.mark.parametrize("code,error", [("NOT_FOUND", FileNotFoundError), ("IS_DIRECTORY", IsADirectoryError),
    ("NOT_DIRECTORY", NotADirectoryError), ("PERMISSION_DENIED", PermissionError), ("INVALID_PATH", ValueError)])
def test_filesystem_maps_control_plane_errors(code, error):
    with pytest.raises(error):
        workspace._file_error(code, "/workspace/test")


async def test_native_grpc_byte_path_and_command_protocol(monkeypatch):
    class Service(rpc.WorkspaceServiceServicer):
        async def Open(self, request, context):
            return pb.WorkspaceStatus(workspace_id=request.request_id, state="open", working_dir="/workspace")

        async def Status(self, request, context):
            return pb.WorkspaceStatus(workspace_id=request.workspace_id, state="open", working_dir="/workspace")

        async def Filesystem(self, request, context):
            assert request.path == "/workspace/bytes" and request.op == "write"
            assert len(request.data) == workspace.MAX_FILE
            return pb.FileReply()

        async def StartCommand(self, request, context):
            assert request.argv == ["sh", "-c", "exit 7"] and not request.shell
            return pb.CommandReply(command_id=request.request_id, state="finished", exit_code=7,
                                   exit_code_available=True, output_complete=True)

    server = grpc.aio.server(options=[("grpc.max_receive_message_length", 32 * 1024 * 1024)])
    rpc.add_WorkspaceServiceServicer_to_server(Service(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    monkeypatch.setattr(workspace, "runtime_settings", lambda: RuntimeSettings(sandbox_address=f"127.0.0.1:{port}"))
    try:
        backend = workspace.SandboxWorkspaceBackend()
        await backend.write_bytes("/workspace/bytes", b"x" * workspace.MAX_FILE)
        assert (await backend.run(["sh", "-c", "exit 7"])).exit_code == 7
    finally:
        await server.stop(0)


async def test_realpath_preserves_symlink_parent_segments_before_remote_resolution():
    backend = StubbedBackend()
    original = backend._rpc
    async def rpc(method, request, **kwargs):
        if method == "Filesystem":
            assert request.op == "realpath" and request.path == "/workspace/link/.."
            return pb.FileReply(path="/")
        return await original(method, request, **kwargs)
    backend._rpc = rpc
    assert await backend.realpath("/workspace/link/..") == "/"


async def test_native_reads_and_writes_preserve_symlink_parent_segments():
    backend = StubbedBackend()
    original = backend._rpc
    observed = []
    async def rpc(method, request, **kwargs):
        if method == "Filesystem":
            observed.append((request.op, request.path))
            return pb.FileReply(data=b"actual target")
        return await original(method, request, **kwargs)
    backend._rpc = rpc
    path = "/workspace/link/../target.txt"
    assert await backend.read_bytes(path) == b"actual target"
    await backend.write_bytes(path, b"updated")
    assert observed == [("read", path), ("write", path)]


async def test_remove_replays_one_mutation_without_client_preflight_and_maps_native_root_guard(monkeypatch):
    backend = StubbedBackend()
    original = backend._rpc
    removed = []
    monkeypatch.setattr(workspace.activity, "info", lambda: SimpleNamespace(workflow_run_id="run", activity_id="remove"))
    async def rpc(method, request, **kwargs):
        if method == "Filesystem":
            assert request.op == "remove"  # no lstat/realpath can fail before cached success replay.
            if request.path == "/tmp/link/..":
                return pb.FileReply(error_code="INVALID_PATH")
            removed.append((request.path, request.request_id))
            return pb.FileReply()
        return await original(method, request, **kwargs)
    backend._rpc = rpc
    with pytest.raises(ValueError):
        await backend.remove("/tmp/link/..")
    first, retry = StubbedBackend(), StubbedBackend()
    first._rpc = retry._rpc = rpc
    await first.remove("/tmp/link")
    await retry.remove("/tmp/link")
    assert removed[0] == removed[1] and removed[0][0] == "/tmp/link"


async def test_destroyed_workspace_after_command_is_not_reported_as_signal_result():
    from pydantic_ai.workspaces import WorkspaceUnavailableError
    backend = StubbedBackend()
    original = backend._rpc
    async def rpc(method, request, **kwargs):
        if method == "Status":
            return pb.WorkspaceStatus(workspace_id="stub", state="closed", cleanup_done=True)
        return await original(method, request, **kwargs)
    backend._rpc = rpc
    with pytest.raises(WorkspaceUnavailableError):
        await backend.run(["sh", "-c", "sleep 1"])


async def test_native_task_cancel_during_create_retains_ref_and_never_opens_twice():
    backend = StubbedBackend()
    original = backend._rpc
    started, release = asyncio.Event(), asyncio.Event()
    async def rpc(method, request, **kwargs):
        if method == "Open":
            started.set()
            await release.wait()
        return await original(method, request, **kwargs)
    backend._rpc = rpc
    caller = asyncio.create_task(backend.working_dir())
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    assert await backend.working_dir() == "/workspace"
    assert backend.ref == WorkspaceRef(provider=workspace.PROVIDER, id="stub")
    assert len([method for method, _ in backend.calls if method == "Open"]) == 1
