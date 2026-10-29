"""True remote POSIX WorkspaceBackend. No workspace path ever touches the host filesystem."""

import asyncio
import hashlib
import json
import math
import posixpath
from collections.abc import Mapping, Sequence
from uuid import uuid4

import anyio
import grpc
from pydantic_ai.workspaces import (CommandResult, FileEntry, WorkspaceOutputLimitError,
    WorkspaceRef, WorkspaceTimeoutError, WorkspaceUnavailableError)
from temporalio import activity

from app.integrations.settings import runtime_settings
from app.integrations.workspace_proto import workspace_pb2 as pb, workspace_pb2_grpc as rpc

PROVIDER = "lazyx-workspace-v1"
MAX_FILE = 8 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024


class WorkspaceProtocolError(RuntimeError):
    pass


def _path(path, *, normalize=True):
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ValueError("Workspace path must be absolute POSIX")
    return posixpath.normpath(path) if normalize else path


def _file_error(code, path):
    errors = {"NOT_FOUND": FileNotFoundError, "MISSING": FileNotFoundError,
              "NOT_DIRECTORY": NotADirectoryError, "IS_DIRECTORY": IsADirectoryError,
              "PERMISSION_DENIED": PermissionError, "ALREADY_EXISTS": FileExistsError,
              "INVALID_PATH": ValueError, "INVALID_ARGUMENT": ValueError,
              "SYMLINK_LOOP": OSError,
              "UNAVAILABLE": WorkspaceUnavailableError, "WORKSPACE_CLOSED": WorkspaceUnavailableError}
    if code:
        raise errors.get(code, OSError)(f"Workspace file operation failed: {code}", path)


class SandboxWorkspaceBackend:
    """One workspace per Coder operation, lazily opened or reattached, never auto-recreated.

    `operation_id=None` is reserved for the conformance harness: it still executes real RPCs,
    merely omitting application database ownership checks. Production capabilities always bind it.
    """

    def __init__(self, *, operation_id=None, workspace_id=None, ref=None, attach=False, allow_finished=False):
        if ref is not None and ref.provider != PROVIDER:
            raise ValueError("Unknown workspace provider")
        if ref is not None and workspace_id is not None and workspace_id != ref.id:
            raise ValueError("Workspace identity mismatch")
        self.operation_id = operation_id
        self.workspace_id = ref.id if ref else workspace_id or "conformance_" + uuid4().hex
        if operation_id is not None and self.workspace_id != "coding_" + operation_id.replace("-", ""):
            raise ValueError("Workspace does not belong to operation")
        self._ref = ref or (WorkspaceRef(provider=PROVIDER, id=self.workspace_id) if attach else None)
        self._attach = attach or ref is not None
        self.allow_finished = allow_finished
        self._lock = anyio.Lock()
        self._ready = False
        self._opening_task = None
        self._ordinal = 0

    @property
    def ref(self):
        return self._ref

    async def _scope(self):
        if self.operation_id is None:
            return {"allowed": True, "remaining_seconds": 1800,
                    "limits": {"deadline_seconds": 1800, "memory_mb": 256}}
        from app.domain.coding_runs import workspace_scope
        result = await workspace_scope(self.operation_id, allow_finished=self.allow_finished)
        if result["workspace_id"] != self.workspace_id or not result["allowed"] and not self.allow_finished:
            raise WorkspaceUnavailableError("Coding run is no longer active")
        return result

    async def _rpc(self, method, request, *, timeout=None):
        settings = runtime_settings()
        async with grpc.aio.insecure_channel(settings.sandbox_address, options=[
                ("grpc.max_send_message_length", 32 * 1024 * 1024),
                ("grpc.max_receive_message_length", 32 * 1024 * 1024)]) as channel:
            try:
                return await getattr(rpc.WorkspaceServiceStub(channel), method)(request,
                    timeout=timeout or settings.sandbox_rpc_timeout_seconds)
            except grpc.aio.AioRpcError as error:
                if error.code() in {grpc.StatusCode.NOT_FOUND, grpc.StatusCode.FAILED_PRECONDITION}:
                    raise WorkspaceUnavailableError("Remote workspace is unavailable") from None
                if error.code() == grpc.StatusCode.PERMISSION_DENIED:
                    raise PermissionError("Remote workspace operation denied") from None
                if error.code() == grpc.StatusCode.INVALID_ARGUMENT:
                    raise ValueError("Invalid remote workspace request") from None
                if error.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    raise WorkspaceOutputLimitError("Remote workspace quota exhausted", limit=MAX_OUTPUT) from None
                # No raw details: a provider error can contain command arguments or source text.
                raise RuntimeError(f"Workspace RPC failed: {error.code().name}") from None

    async def working_dir(self):
        scope = await self._scope()
        async with self._lock:
            if not self._ready:
                if self._opening_task is None:
                    self._opening_task = asyncio.create_task(self._initialize(scope))
                    self._opening_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                # Shield both AnyIO level cancellation and native asyncio.Task.cancel(). The
                # initializer records the returned ref even if this particular caller is gone.
                with anyio.CancelScope(shield=True):
                    await asyncio.shield(self._opening_task)
        return "/workspace"

    async def _initialize(self, scope):
        # Once a ref was given, only Status is allowed: no resurrection after destruction.
        remote = await self._rpc("Status", pb.WorkspaceRef(workspace_id=self.workspace_id)) if self._attach else await self._rpc(
            "Open", pb.OpenWorkspaceRequest(request_id=self.workspace_id,
                ttl_seconds=scope["limits"]["deadline_seconds"], memory_mb=scope["limits"]["memory_mb"]), timeout=90)
        if remote.workspace_id != self.workspace_id:
            raise WorkspaceProtocolError("Workspace identity mismatch")
        self._ref = WorkspaceRef(provider=PROVIDER, id=self.workspace_id)
        if self.operation_id:
            from app.domain.coding_runs import workspace_opened
            await workspace_opened(self.operation_id, self.workspace_id)
        if remote.state != "open" or remote.working_dir != "/workspace":
            raise WorkspaceUnavailableError("Remote workspace is not open")
        self._attach, self._ready = True, True

    def _request_id(self, payload):
        self._ordinal += 1
        try:
            info = activity.info()
            identity = f"{info.workflow_run_id}:{info.activity_id}:{self._ordinal}"
        except RuntimeError:
            identity = uuid4().hex
        return hashlib.sha256((identity + json.dumps(payload, sort_keys=True, default=str)).encode()).hexdigest()

    async def _file(self, op, path, **kwargs):
        await self.working_dir()
        # Leave symlink/.. traversal to the remote kernel, including for native reads/writes.
        # Workspace.resolve may normalize an agent's input; a direct backend call must not.
        path = _path(path, normalize=False)
        remote = await self._rpc("Filesystem", pb.FileRequest(workspace_id=self.workspace_id, op=op, path=path,
            request_id=self._request_id({"op": op, "path": path, "digest": hashlib.sha256(kwargs.get("data", b"")).hexdigest()}),
            **kwargs))
        _file_error(remote.error_code, path)
        return remote

    async def read_bytes(self, path):
        data = bytes((await self._file("read", path)).data)
        if len(data) > MAX_FILE:
            raise WorkspaceOutputLimitError("File exceeds read limit", limit=MAX_FILE)
        return data

    async def write_bytes(self, path, data):
        if len(data) > MAX_FILE:
            raise ValueError("Workspace file exceeds 8 MiB")
        await self._file("write", path, data=data, parents=True)

    @staticmethod
    def _entry(entry):
        path = _path(entry.path)
        if posixpath.basename(path) != entry.name:
            raise WorkspaceProtocolError("Invalid file entry")
        return FileEntry(name=entry.name, path=path, is_dir=entry.is_dir, size=entry.size if entry.kind == "file" else None)

    async def stat(self, path):
        result = await self._file("stat", path)
        if len(result.entries) != 1:
            raise WorkspaceProtocolError("Missing stat entry")
        return self._entry(result.entries[0])

    async def list_dir(self, path):
        result = await self._file("list", path)
        if len(result.entries) > 5000:
            raise WorkspaceOutputLimitError("Directory exceeds entry limit", limit=5000)
        return [self._entry(item) for item in result.entries]

    async def make_dir(self, path):
        await self._file("mkdir", path, parents=True)

    async def remove(self, path):
        path = _path(path, normalize=False)
        if path in {"/", "/workspace"}:
            raise ValueError("Refusing to remove working directory or ancestor")
        # Dynamic ancestor protection is enforced by the trusted native helper, after the
        # node's mutation ledger has replayed any earlier successful result. A client-side
        # lstat would turn an acknowledged-but-lost successful remove into MISSING on retry.
        await self._file("remove", path, recursive=True)

    async def exists(self, path):
        try:
            await self.stat(path)
            return True
        except FileNotFoundError:
            return False

    async def realpath(self, path):
        remote = await self._file("realpath", path)
        result = _path(remote.path)
        if result != remote.path:
            raise WorkspaceProtocolError("Noncanonical realpath")
        return result

    async def _record(self, command_id, command, remote):
        if self.operation_id:
            from app.domain.coding_runs import record_command
            await record_command(self.operation_id, command_id, command, remote)

    def _command(self, result, command_id):
        if result.command_id != command_id or result.state not in {"starting", "running", "finished"}:
            raise WorkspaceProtocolError("Invalid command identity or state")
        if len(result.stdout) > MAX_OUTPUT or len(result.stderr) > MAX_OUTPUT:
            raise WorkspaceProtocolError("Command output exceeds protocol cap")
        return {"state": result.state, "exit_code": result.exit_code if result.exit_code_available else None,
                "stdout": result.stdout.decode("utf-8", "replace"), "stderr": result.stderr.decode("utf-8", "replace")}

    async def run(self, command: Sequence[str] | str, *, shell=False, env: Mapping[str, str] | None = None, timeout=None):
        if isinstance(command, str) != shell:
            raise TypeError("String requires shell=True; argv requires shell=False")
        if not command or not isinstance(command, (str, Sequence)):
            raise ValueError("Empty command")
        if not isinstance(command, str) and any(not isinstance(item, str) for item in command):
            raise TypeError("argv entries must be strings")
        if timeout is not None and (not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be positive and finite")
        await self.working_dir()
        scope = await self._scope()
        duration = min(timeout or 120, 120)
        if scope["remaining_seconds"] <= 0:
            raise WorkspaceTimeoutError("Coding run deadline exceeded")
        command_id = self._request_id({"command": command, "shell": shell, "env": dict(env or {}), "timeout": duration})
        request = pb.CommandRequest(workspace_id=self.workspace_id, request_id=command_id,
            argv=[] if shell else list(command), shell_command=command if shell else "", shell=shell,
            cwd="/workspace", env=dict(env or {}), timeout_seconds=max(1, math.ceil(duration)), output_limit_bytes=MAX_OUTPUT)
        reference = pb.CommandRef(workspace_id=self.workspace_id, command_id=command_id)
        display = command if isinstance(command, str) else json.dumps(list(command), ensure_ascii=False)
        finished = False
        await self._record(command_id, display, {"state": "running", "exit_code": None, "stdout": "", "stderr": ""})
        try:
            remote = await self._rpc("StartCommand", request)
            recorded = None
            with anyio.move_on_after(duration + 2) as deadline:
                while True:
                    value = self._command(remote, command_id)
                    if value != recorded:
                        await self._record(command_id, display, value)
                        recorded = value
                    if remote.state == "finished":
                        finished = True
                        break
                    await anyio.sleep(0.1)
                    await self._scope()
                    remote = await self._rpc("PollCommand", reference)
            if deadline.cancel_called:
                raise WorkspaceTimeoutError("Command deadline exceeded", stdout=value["stdout"], stderr=value["stderr"])
            live = await self._rpc("Status", pb.WorkspaceRef(workspace_id=self.workspace_id))
            if live.workspace_id != self.workspace_id or live.state != "open":
                raise WorkspaceUnavailableError("Workspace destroyed during command")
            if remote.error_code in {"WORKSPACE_CLOSED", "UNAVAILABLE"}:
                raise WorkspaceUnavailableError("Workspace closed during command")
            if remote.timed_out:
                raise WorkspaceTimeoutError("Command timed out", stdout=value["stdout"], stderr=value["stderr"])
            if remote.output_truncated:
                raise WorkspaceOutputLimitError("Command output exceeded cap", limit=MAX_OUTPUT,
                                                stdout=value["stdout"], stderr=value["stderr"])
            if not remote.output_complete or value["exit_code"] is None:
                raise WorkspaceProtocolError("Command has no trusted complete result")
            return CommandResult(exit_code=value["exit_code"], stdout=value["stdout"], stderr=value["stderr"])
        finally:
            if not finished:
                with anyio.CancelScope(shield=True):
                    try:
                        remote = await self._rpc("CancelCommand", reference)
                        with anyio.move_on_after(5):
                            while remote.state != "finished":
                                await anyio.sleep(0.1)
                                remote = await self._rpc("PollCommand", reference)
                        await self._record(command_id, display, self._command(remote, command_id))
                    except Exception:
                        pass  # Workspace TTL and owner Close are the final cleanup boundary.

    async def export_files(self):
        await self.working_dir()
        pending, files, seen, total = ["/workspace"], [], 0, 0
        while pending:
            directory = pending.pop()
            result = await self._file("list", directory)
            for entry in sorted(result.entries, key=lambda item: item.path):
                seen += 1
                if seen > 5000:
                    raise WorkspaceOutputLimitError("Export directory limit exceeded", limit=5000)
                path = _path(entry.path)
                if posixpath.dirname(path) != directory or path != entry.path:
                    raise WorkspaceProtocolError("Export entry outside requested directory")
                relative = path.removeprefix("/workspace/")
                if any(part.startswith(".") or part == "__pycache__" for part in relative.split("/")):
                    continue
                # Symlinks, devices and FIFOs are never exported; recheck realpath for races.
                if entry.kind == "directory":
                    pending.append(path)
                elif entry.kind == "file":
                    if len(files) >= 8 or entry.size > MAX_FILE or total + entry.size > 16 * 1024 * 1024:
                        raise WorkspaceOutputLimitError("Generated files exceed export quota", limit=16 * 1024 * 1024)
                    if await self.realpath(path) != path:
                        continue
                    data = await self.read_bytes(path)
                    total += len(data)
                    if total > 16 * 1024 * 1024:
                        raise WorkspaceOutputLimitError("Generated files exceed export quota", limit=16 * 1024 * 1024)
                    files.append((relative, data))
        return files

    async def close(self):
        remote = await self._rpc("Close", pb.WorkspaceRef(workspace_id=self.workspace_id), timeout=90)
        if remote.workspace_id != self.workspace_id or not remote.cleanup_done:
            raise WorkspaceProtocolError("Workspace cleanup is not confirmed")
        self._ready = False
