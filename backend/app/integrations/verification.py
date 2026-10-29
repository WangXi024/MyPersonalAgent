"""Dedicated scoped WorkspaceService transport; preserves trusted command bytes and metadata."""

import hashlib

import grpc

from app.integrations.settings import runtime_settings
from app.integrations.workspace_proto import workspace_pb2 as pb, workspace_pb2_grpc as rpc

MAX_OUTPUT = 65536
COMMAND = "exec env -i PATH=/usr/local/bin:/usr/bin:/bin python3 -I -S /workspace/source.py < /workspace/stdin.bin"


class VerificationServiceError(RuntimeError):
    def __init__(self, code="service_unavailable"):
        self.code = code
        super().__init__(code)


class VerificationTransport:
    """All requests authenticate a persisted operation/run/case, including cleanup."""

    def __init__(self, operation_id, case_id):
        if not operation_id or not case_id:
            raise ValueError("A verification operation and case are required")
        self.operation_id, self.case_id = operation_id, case_id

    async def _call(self, method, make_request):
        from app.domain.verification import transport_scope
        scope = await transport_scope(self.operation_id, self.case_id, action=method,
                                      cleanup=method in {"Status", "Close", "PollCommand", "CancelCommand"})
        request = make_request(scope)
        settings = runtime_settings()
        try:
            async with grpc.aio.insecure_channel(settings.sandbox_address, options=[
                ("grpc.max_send_message_length", 32 * 1024 * 1024),
                ("grpc.max_receive_message_length", 32 * 1024 * 1024)]) as channel:
                response = await getattr(rpc.WorkspaceServiceStub(channel), method)(request,
                    timeout=90 if method == "Open" else settings.sandbox_rpc_timeout_seconds)
        except grpc.aio.AioRpcError as error:
            raise VerificationServiceError("command_unavailable" if method in {"PollCommand", "CancelCommand"}
                and error.code() == grpc.StatusCode.NOT_FOUND else "workspace_unavailable"
                if error.code() in {grpc.StatusCode.NOT_FOUND, grpc.StatusCode.FAILED_PRECONDITION} else "service_unavailable") from None
        if method in {"Open", "Status", "Close"}:
            if (response.workspace_id != scope["workspace_id"] or response.state not in {"opening", "open", "closing", "closed", "failed"}
                    or response.cleanup_done and response.state != "closed"
                    or response.state == "open" and response.working_dir != "/workspace"):
                raise VerificationServiceError("protocol_error")
        elif method in {"StartCommand", "PollCommand", "CancelCommand"}:
            if (response.command_id != scope["command_id"] or response.state not in {"starting", "running", "finished"}
                    or len(response.stdout) > MAX_OUTPUT or len(response.stderr) > MAX_OUTPUT):
                raise VerificationServiceError("protocol_error")
        elif response.error_code:
            raise VerificationServiceError("service_unavailable")
        return response

    async def open(self):
        return await self._call("Open", lambda scope: pb.OpenWorkspaceRequest(request_id=scope["workspace_id"],
                                                                               ttl_seconds=600, memory_mb=256))

    async def status(self):
        return await self._call("Status", lambda scope: pb.WorkspaceRef(workspace_id=scope["workspace_id"]))

    async def close(self):
        return await self._call("Close", lambda scope: pb.WorkspaceRef(workspace_id=scope["workspace_id"]))

    async def write(self, path, data):
        if path not in {"/workspace/source.py", "/workspace/stdin.bin"} or len(data) > 1024 * 1024:
            raise VerificationServiceError("protocol_error")
        def request(scope):
            if hashlib.sha256(data).hexdigest() != scope["file_hashes"][path]:
                raise VerificationServiceError("protocol_error")
            return pb.FileRequest(workspace_id=scope["workspace_id"],
                request_id=hashlib.sha256((scope["workspace_id"] + path).encode() + data).hexdigest(),
                op="write", path=path, data=data, parents=True)
        return await self._call("Filesystem", request)

    async def start(self):
        return await self._call("StartCommand", lambda scope: pb.CommandRequest(workspace_id=scope["workspace_id"],
            request_id=scope["command_id"], shell=True, shell_command=COMMAND, cwd="/workspace",
            timeout_seconds=10, output_limit_bytes=MAX_OUTPUT))

    async def poll(self):
        return await self._call("PollCommand", lambda scope: pb.CommandRef(workspace_id=scope["workspace_id"], command_id=scope["command_id"]))

    async def cancel(self):
        return await self._call("CancelCommand", lambda scope: pb.CommandRef(workspace_id=scope["workspace_id"], command_id=scope["command_id"]))
