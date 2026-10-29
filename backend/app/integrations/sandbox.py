"""Minimal asynchronous client for the versioned, independent sandbox protocol."""

import hashlib
import re
from pathlib import PurePosixPath

import grpc

from app.integrations.sandbox_proto import sandbox_pb2 as pb, sandbox_pb2_grpc as rpc
from app.integrations.settings import runtime_settings

PHASES = {"queued", "preparing", "creating", "running", "returning", "finished"}
OUTCOMES = {"", "success", "failed", "cancelled", "unknown"}


class SandboxRPCError(RuntimeError):
    def __init__(self, code: str, reason: str = ""):
        self.code = code
        self.reason = reason if re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", reason) else ""
        super().__init__(f"Sandbox request failed: {code}")


class SandboxProtocolError(RuntimeError):
    pass


def execution_request(request_id: str, code: str, limits: dict):
    return pb.TaskRequest(request_id=request_id, kind="code", environment="python", code=code.encode("utf-8"),
                          memory_mb=limits["memory_mb"], timeout_seconds=limits["timeout_seconds"])


def request_digest(request) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


def status_dict(status, request_id: str) -> dict:
    if status.request_id != request_id or status.phase not in PHASES or status.outcome not in OUTCOMES:
        raise SandboxProtocolError("Invalid sandbox status")
    if status.phase == "finished" and not status.outcome:
        raise SandboxProtocolError("Finished sandbox status has no outcome")
    code = status.error_code
    if code and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", code):
        code = "EXECUTION_FAILED"
    return {"request_id": request_id, "phase": status.phase, "outcome": status.outcome,
            "error_code": code or None, "cleanup_done": status.cleanup_done,
            "delivery_done": status.delivery_done, "resource_usage": {
                "cpu_usage_usec": status.cpu_usage_usec, "peak_memory_bytes": status.peak_memory_bytes,
                "cpu_usage_available": status.cpu_usage_available,
                "peak_memory_available": status.peak_memory_available,
                "resource_usage_final": status.resource_usage_final}}


def validated_result(result, request_id: str) -> dict:
    status = status_dict(result.status, request_id)
    if status["phase"] != "finished":
        raise SandboxProtocolError("Sandbox result is not finished")
    if len(result.stdout.encode("utf-8")) > 65536 or len(result.stderr.encode("utf-8")) > 65536:
        raise SandboxProtocolError("Sandbox logs exceed limits")
    if len(result.artifacts) > 8:
        raise SandboxProtocolError("Too many sandbox artifacts")
    artifacts, seen, total = [], set(), 0
    for artifact in result.artifacts:
        name = artifact.path
        path = PurePosixPath(name)
        size = len(artifact.data)
        total += size
        if (not name or not path.parts or len(name) > 1024 or path.is_absolute() or ".." in path.parts
                or "\\" in name or ":" in name or str(path) != name or name in seen
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or size > 2 * 1024 * 1024 or total > 4 * 1024 * 1024
                or hashlib.sha256(artifact.data).hexdigest() != artifact.sha256):
            raise SandboxProtocolError("Sandbox artifact validation failed")
        seen.add(name)
        artifacts.append({"filename": name, "data": bytes(artifact.data), "size": size,
                          "sha256": artifact.sha256})
    # proposal_json is deliberately ignored. It never authorizes product-side actions.
    return {"status": status, "stdout": result.stdout, "stderr": result.stderr, "artifacts": artifacts}


class SandboxClient:
    def __init__(self):
        settings = runtime_settings()
        self.timeout = settings.sandbox_rpc_timeout_seconds
        self.channel = grpc.aio.insecure_channel(settings.sandbox_address, options=[
            ("grpc.max_send_message_length", 8 * 1024 * 1024),
            ("grpc.max_receive_message_length", 8 * 1024 * 1024)])
        self.stub = rpc.SandboxServiceStub(self.channel)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.channel.close()

    async def _call(self, name, request):
        try:
            return await getattr(self.stub, name)(request, timeout=self.timeout)
        except grpc.aio.AioRpcError as error:
            raise SandboxRPCError(error.code().name, error.details() or "") from None

    async def ready(self) -> dict:
        status = await self._call("Ready", pb.Empty())
        return {"ready": status.ready, "accepting_tasks": status.accepting_tasks,
                "independent": status.mode == "independent", "environments": list(status.environments)}

    async def submit(self, request) -> dict:
        return status_dict(await self._call("Submit", request), request.request_id)

    async def status(self, request_id: str) -> dict:
        return status_dict(await self._call("Status", pb.JobRef(request_id=request_id)), request_id)

    async def cancel(self, request_id: str) -> dict:
        return status_dict(await self._call("Cancel", pb.JobRef(request_id=request_id)), request_id)

    async def result(self, request_id: str) -> dict:
        return validated_result(await self._call("Result", pb.JobRef(request_id=request_id)), request_id)

    async def acknowledge(self, request_id: str) -> dict:
        return status_dict(await self._call("Acknowledge", pb.JobRef(request_id=request_id)), request_id)
