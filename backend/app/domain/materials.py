"""Explicit, owned, offline material extraction. No learning or model side effects."""

from copy import deepcopy
from datetime import timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Attachment, Operation, Outbox, now
from app.db.session import SessionFactory
from app.domain.common import DomainError, emit, lock_user, new_operation, owned
from app.domain.execution import operation_locked
from app.integrations.materials import MAX_INPUT, MAX_JSON, PARSER_VERSION, MaterialContentError, parse_request, parsed_document
from app.integrations.sandbox import SandboxClient, SandboxProtocolError, SandboxRPCError, request_digest
from app.integrations.settings import runtime_settings


MIMES = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
FINAL = {"succeeded", "failed", "cancelled", "unavailable"}
ERRORS = {"disabled": "资料解析尚未启用", "account_inactive": "账号已停用，正在停止解析",
    "deadline_exceeded": "解析已超过时间上限", "source_changed": "原始资料校验失败，请重新上传",
    "parse_failed": "文档损坏、加密或超出解析范围，未获得可用正文",
    "no_text": "未提取到正文；扫描件需要OCR，当前不支持",
    "invalid_result": "解析服务返回的数据未通过校验", "result_expired": "原任务结果已过期，未自动重复解析",
    "service_unavailable": "解析服务暂不可用，执行与清理状态尚未确认",
    "job_unknown": "原任务状态无法确认，未自动重复解析"}


def _aware(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def capabilities():
    return {"enabled": runtime_settings().material_enabled, "extensions": [".pdf", ".docx"],
        "max_upload_bytes": MAX_INPUT, "max_daily": 5, "max_concurrent": 1, "runtime_seconds": 30,
        "deadline_seconds": 240, "ocr": False, "model_required": False}


def path_for(key):
    relative = PurePosixPath(key)
    base = get_settings().attachment_dir.resolve()
    if relative.is_absolute() or ".." in relative.parts or "\\" in key or ":" in key:
        raise DomainError("file_unavailable", "资料文件暂不可用", 503)
    path = (base / key).resolve()
    if path == base or not path.is_relative_to(base):
        raise DomainError("file_unavailable", "资料文件暂不可用", 503)
    return path


def _atomic(key, content):
    target = path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".material-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read(attachment, limit):
    try:
        with path_for(attachment.storage_key).open("rb") as handle:
            content = handle.read(limit + 1)
    except OSError:
        raise DomainError("file_unavailable", "资料文件暂不可用", 503) from None
    if len(content) != attachment.size or len(content) > limit or hashlib.sha256(content).hexdigest() != attachment.sha256:
        raise DomainError("source_changed", ERRORS["source_changed"], 409)
    return content


def _state(op, material):
    return {"operation_id": op.id, "job_id": material["request_id"], "status": material["status"],
        "phase": material["phase"], "terminal": bool(material["quota_released"] or material["halted"]),
        "submitted": material["submit_started"], "result_saved": material["result_saved"],
        "needs_ack": material["result_saved"] and material["submit_started"] and not material["delivery_done"],
        "cleanup_done": material["cleanup_done"], "delivery_done": material["delivery_done"],
        "cancel_requested": material["cancel_requested"],
        "remaining_seconds": max(0, (_aware(op.created_at) + timedelta(seconds=240) - now()).total_seconds())}


def public(op):
    value = op.result["material"]
    identifier = op.id
    downloads = [{"format": kind, "filename": item["filename"], "size": item["size"],
        "download_url": f"/api/v1/materials/{identifier}/download/{kind}"}
        for kind, item in value["derived"].items()] if value["status"] == "succeeded" else []
    return {"id": identifier, "operation_id": identifier, "filename": op.payload["filename"],
        "media_type": op.payload["mime"], "size": op.payload["size"], "sha256": op.payload["sha256"],
        "created_at": _aware(op.created_at).isoformat(), "updated_at": _aware(op.updated_at).isoformat(),
        **{name: value[name] for name in ("status", "phase", "result_saved", "cleanup_done", "delivery_done",
            "cancel_requested", "error_code", "error_message", "extraction")},
        "can_cancel": not value["quota_released"] and not value["halted"],
        "preview_url": f"/api/v1/materials/{identifier}/preview" if downloads else None, "downloads": downloads}


async def _owned(db, operation_id, user_id):
    op = await owned(db, Operation, operation_id, user_id)
    if op.kind != "material_parse" or not isinstance(op.result.get("material"), dict):
        raise DomainError("not_found", "资料不存在", 404)
    return op


async def _locked(db, operation_id):
    op, user, stream, _ = await operation_locked(db, operation_id)
    await _owned(db, op.id, user.id)
    return op, user, stream, deepcopy(op.result["material"])


async def _save(db, op, stream, material):
    material["quota_released"] = bool(material["result_saved"] and material["cleanup_done"] and material["delivery_done"])
    status = "running" if material["status"] == "cancelling" else material["status"]
    stage = "saved" if material["result_saved"] else material["phase"]
    if op.result != {"material": material} or op.status != status or op.stage != stage:
        op.result, op.status, op.stage, op.updated_at = {"material": material}, status, stage, now()
        op.error_code, op.error_message = material["error_code"], material["error_message"]
        await emit(db, stream, "material.updated", op.id, "资料解析状态已更新", operation_id=op.id)
    return _state(op, material)


def _local_finish(material, status, code=None):
    material.update(status=status, phase="finished", result_saved=True, cleanup_done=True, delivery_done=True)
    if code:
        material.update(error_code=code, error_message=ERRORS[code])


async def create_material(user_id, filename, content, key):
    filename = (filename or "document").replace("\\", "/").rsplit("/", 1)[-1][:255]
    source_type = Path(filename).suffix.lower().lstrip(".")
    if source_type not in MIMES or any(ord(char) < 32 or ord(char) == 127 for char in filename):
        raise DomainError("invalid_file_type", "资料解析仅支持PDF或DOCX文件", 422)
    if not content or len(content) > MAX_INPUT:
        raise DomainError("material_too_large", "资料解析文件需为1字节至1 MiB，请拆分后上传", 413)
    if not (content.startswith(b"%PDF-") if source_type == "pdf" else content.startswith((b"PK\x03\x04", b"PK\x05\x06"))):
        raise DomainError("invalid_file", "资料文件头与类型不符", 422)
    sha = hashlib.sha256(content).hexdigest()
    storage_key = str(uuid4())
    stored = False
    try:
        async with SessionFactory() as db, db.begin():
            user, stream = await lock_user(db, user_id)
            if not user.active:
                raise DomainError("unauthenticated", "账号已停用", 401)
            op, created = await new_operation(db, user, stream, key, "material_parse",
                {"filename": filename, "mime": MIMES[source_type], "size": len(content), "sha256": sha})
            if not created:
                return public(op)
            if not runtime_settings().material_enabled:
                raise DomainError("material_disabled", ERRORS["disabled"], 503)
            active = await db.scalar(select(func.count()).select_from(Operation).where(
                Operation.user_id == user_id, Operation.kind == "material_parse", Operation.id != op.id,
                Operation.result["material"]["quota_released"].as_boolean().is_not(True)))
            if active:
                raise DomainError("material_busy", "已有资料正在解析或等待清理确认，请稍后再试", 429)
            daily = await db.scalar(select(func.count()).select_from(Operation).where(
                Operation.user_id == user_id, Operation.kind == "material_parse",
                Operation.created_at >= now() - timedelta(days=1)))
            if daily > 5:
                raise DomainError("material_daily_limit", "最近24小时的五次资料解析额度已用完", 429)
            _atomic(storage_key, content)
            attachment = Attachment(user_id=user_id, storage_key=storage_key, filename=filename,
                                    mime=MIMES[source_type], size=len(content), sha256=sha)
            db.add(attachment)
            await db.flush()
            request_id = "material_" + op.id.replace("-", "")
            request = parse_request(request_id, source_type, content, sha)
            material = {"source_attachment_id": attachment.id, "source_type": source_type,
                "request_id": request_id, "request_hash": request_digest(request), "parser_version": PARSER_VERSION,
                "status": "accepted", "phase": "queued", "submit_started": False, "submit_attempts": 0,
                "result_saved": False, "delivery_done": False, "cleanup_done": False, "quota_released": False,
                "cancel_requested": False, "halted": False, "error_code": None, "error_message": None,
                "extraction": None, "derived": {}, "receipt": None}
            op.result = {"material": material}
            db.add(Outbox(user_id=user_id, operation_id=op.id, kind="temporal", dedupe_key=f"material:{op.id}",
                payload={"type": "material_parse", "user_id": user_id, "operation_id": op.id}))
            await db.flush()
            response = public(op)
        stored = True
        return response
    finally:
        if not stored:
            path_for(storage_key).unlink(missing_ok=True)


async def list_materials(user_id):
    async with SessionFactory() as db:
        rows = await db.scalars(select(Operation).where(Operation.user_id == user_id, Operation.kind == "material_parse")
            .order_by(Operation.created_at.desc()).limit(30))
        return {"items": [public(op) for op in rows]}


async def get_material(user_id, operation_id):
    async with SessionFactory() as db:
        return public(await _owned(db, operation_id, user_id))


async def download(user_id, operation_id, kind):
    async with SessionFactory() as db:
        op = await _owned(db, operation_id, user_id)
        material = op.result["material"]
        item = material["derived"].get(kind) if kind in {"txt", "json"} else None
        if material["status"] != "succeeded" or not item:
            raise DomainError("not_found", "解析产物不存在", 404)
        attachment = await owned(db, Attachment, item["attachment_id"], user_id)
        return _read(attachment, MAX_JSON), item["filename"], attachment.mime


async def preview(user_id, operation_id):
    data, _, _ = await download(user_id, operation_id, "txt")
    text = data.decode("utf-8")
    item = await get_material(user_id, operation_id)
    return {"material_id": operation_id, "text": text[:32000], "char_count": len(text), "truncated": len(text) > 32000,
        "type": item["extraction"]["type"], "page_count": item["extraction"]["page_count"], "untrusted": True}


async def request_cancel(user_id, operation_id, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        op = await _owned(db, operation_id, user_id)
        command, created = await new_operation(db, user, stream, key, "material_cancel", {"material_id": op.id})
        if created:
            command.status, command.stage, command.result = "succeeded", "saved", {"material_id": op.id}
            material = deepcopy(op.result["material"])
            if not material["quota_released"] and not material["halted"]:
                material["cancel_requested"] = True
                if not material["result_saved"]:
                    material["status"] = "cancelling"
                await _save(db, op, stream, material)
                db.add(Outbox(user_id=user_id, operation_id=op.id, kind="temporal",
                    dedupe_key=f"material-cancel:{op.id}", payload={"type": "material_parse_cancelled",
                        "user_id": user_id, "operation_id": op.id}))
        return public(op)


async def prepare_material(operation_id):
    async with SessionFactory() as db, db.begin():
        op, user, stream, material = await _locked(db, operation_id)
        if not material["result_saved"] and not material["halted"]:
            code = ("account_inactive" if not user.active else "disabled" if not runtime_settings().material_enabled else
                    "deadline_exceeded" if now() >= _aware(op.created_at) + timedelta(seconds=240) else None)
            if code or op.status in {"cancelled", "superseded"}:
                material["cancel_requested"] = True
                if code:
                    material.update(error_code=code, error_message=ERRORS[code])
            if material["cancel_requested"]:
                if not material["submit_started"]:
                    _local_finish(material, "failed" if code else "cancelled", code)
                else:
                    material["status"] = "cancelling"
        return await _save(db, op, stream, material)


async def _record_remote(operation_id, remote):
    async with SessionFactory() as db, db.begin():
        op, _, stream, material = await _locked(db, operation_id)
        if material["phase"] == "finished" and remote["phase"] != "finished":
            raise SandboxProtocolError("Material phase regressed")
        material["phase"] = remote["phase"]
        material["cleanup_done"] |= remote["cleanup_done"]
        material["delivery_done"] |= remote["delivery_done"]
        if not material["result_saved"]:
            material["status"] = "cancelling" if material["cancel_requested"] else "running"
        return await _save(db, op, stream, material)


async def submit_material(operation_id):
    state = await prepare_material(operation_id)
    if state["terminal"] or state["cancel_requested"]:
        return state
    # Reattachment is a read. Unknown status after a lost reply never creates a second ID.
    if state["submitted"]:
        return await poll_material(operation_id)
    async with SandboxClient() as client:
        ready = await client.ready()
        if not ready["ready"] or not ready["independent"] or "files" not in ready["environments"]:
            raise SandboxProtocolError("Material environment unavailable")
        if not ready["accepting_tasks"]:
            raise SandboxRPCError("RESOURCE_EXHAUSTED")
    async with SessionFactory() as db, db.begin():
        op, user, stream, material = await _locked(db, operation_id)
        if (material["cancel_requested"] or op.status in {"cancelled", "superseded"} or not user.active
                or not runtime_settings().material_enabled or now() >= _aware(op.created_at) + timedelta(seconds=240)):
            material["cancel_requested"] = True
            if not material["submit_started"]:
                code = ("account_inactive" if not user.active else "disabled" if not runtime_settings().material_enabled
                        else "deadline_exceeded" if now() >= _aware(op.created_at) + timedelta(seconds=240) else None)
                _local_finish(material, "failed" if code else "cancelled", code)
            return await _save(db, op, stream, material)
        if material["submit_started"]:
            return _state(op, material)
        attachment = await owned(db, Attachment, material["source_attachment_id"], user.id)
        content = _read(attachment, MAX_INPUT)
        request = parse_request(material["request_id"], material["source_type"], content, op.payload["sha256"])
        if request_digest(request) != material["request_hash"]:
            raise SandboxProtocolError("Material request changed")
        material["submit_started"], material["submit_attempts"] = True, material["submit_attempts"] + 1
        await _save(db, op, stream, material)
    async with SandboxClient() as client:
        remote = await client.submit(request)
    return await _record_remote(operation_id, remote)


async def poll_material(operation_id):
    state = await prepare_material(operation_id)
    if (state["cleanup_done"] and state["result_saved"]) or not state["submitted"]:
        return state
    async with SandboxClient() as client:
        try:
            remote = await client.status(state["job_id"])
        except SandboxRPCError as error:
            if error.code in {"NOT_FOUND", "FAILED_PRECONDITION"}:
                return await fail_material(operation_id, "job_unknown")
            raise
    return await _record_remote(operation_id, remote)


async def collect_material(operation_id):
    state = await prepare_material(operation_id)
    if state["result_saved"] or state["terminal"]:
        return state
    async with SandboxClient() as client:
        try:
            result = await client.result(state["job_id"])
        except SandboxRPCError as error:
            if error.code in {"NOT_FOUND", "FAILED_PRECONDITION"}:
                return await fail_material(operation_id, "result_expired")
            raise
    async with SessionFactory() as db, db.begin():
        op, user, stream, material = await _locked(db, operation_id)
        if material["result_saved"]:
            return _state(op, material)
        remote = result["status"]
        if remote["phase"] != "finished" or remote["outcome"] == "unknown":
            raise SandboxProtocolError("Material result not confirmed")
        material["phase"], material["cleanup_done"] = "finished", material["cleanup_done"] or remote["cleanup_done"]
        material["receipt"] = {"outcome": remote["outcome"], "error_code": remote["error_code"],
            "artifacts": [{"sha256": artifact["sha256"], "size": artifact["size"]} for artifact in result["artifacts"]]}
        cancelled = (material["cancel_requested"] or op.status in {"cancelled", "superseded"} or not user.active
                     or not runtime_settings().material_enabled or now() >= _aware(op.created_at) + timedelta(seconds=240))
        if cancelled:
            material["cancel_requested"] = True
        if cancelled or remote["outcome"] == "cancelled":
            material["status"] = "cancelled"
        elif remote["outcome"] != "success":
            material.update(status="failed", error_code="parse_failed", error_message=ERRORS["parse_failed"])
        else:
            try:
                parsed, text = parsed_document(result, material["source_type"])
            except MaterialContentError:
                # A validated body was received: save its failed validation receipt before ACK.
                material.update(status="failed", error_code="invalid_result", error_message=ERRORS["invalid_result"])
                material["result_saved"] = True
                return await _save(db, op, stream, material)
            material["extraction"] = {"type": material["source_type"], "page_count": parsed.get("page_count"),
                "char_count": len(text), "no_text": not bool(text.strip()), "preview_truncated": len(text) > 32000}
            if not text.strip():
                material.update(status="failed", error_code="no_text", error_message=ERRORS["no_text"])
            else:
                output = {"schema_version": 1, "source": op.payload, "parser_version": PARSER_VERSION,
                          "untrusted": True, "document": parsed}
                content = {"txt": text.encode("utf-8"), "json": json.dumps(output, ensure_ascii=False, indent=2).encode("utf-8")}
                for kind, data in content.items():
                    if len(data) > MAX_JSON:
                        raise SandboxProtocolError("Derived material exceeds limit")
                    identifier = str(uuid5(NAMESPACE_URL, f"material:{op.id}:{kind}"))
                    filename = f"material-{op.id}.{kind}"
                    storage_key = f"materials/{op.id}/{identifier}"
                    sha = hashlib.sha256(data).hexdigest()
                    _atomic(storage_key, data)
                    row = await db.get(Attachment, identifier)
                    if row is None:
                        row = Attachment(id=identifier, user_id=user.id, storage_key=storage_key, filename=filename,
                            mime="text/plain" if kind == "txt" else "application/json", size=len(data), sha256=sha)
                        db.add(row)
                    elif row.user_id != user.id or row.sha256 != sha or row.storage_key != storage_key:
                        raise SandboxProtocolError("Material artifact identity changed")
                    material["derived"][kind] = {"attachment_id": identifier, "filename": filename, "size": len(data)}
                material["status"] = "succeeded"
        material["result_saved"] = True
        return await _save(db, op, stream, material)


async def ack_material(operation_id):
    state = await prepare_material(operation_id)
    if not state["needs_ack"]:
        return state
    async with SandboxClient() as client:
        remote = await client.acknowledge(state["job_id"])
    return await _record_remote(operation_id, remote)


async def cancel_material(operation_id):
    async with SessionFactory() as db, db.begin():
        op, _, stream, material = await _locked(db, operation_id)
        material["cancel_requested"] = True
        await _save(db, op, stream, material)
    state = await prepare_material(operation_id)
    if state["terminal"] or not state["submitted"]:
        return state
    async with SandboxClient() as client:
        try:
            remote = await client.cancel(state["job_id"])
        except SandboxRPCError as error:
            if error.code == "NOT_FOUND":
                return await fail_material(operation_id, "job_unknown")
            raise
    return await _record_remote(operation_id, remote)


async def fail_material(operation_id, code):
    code = code if code in ERRORS else "service_unavailable"
    async with SessionFactory() as db, db.begin():
        op, _, stream, material = await _locked(db, operation_id)
        if not material["result_saved"]:
            if not material["submit_started"]:
                _local_finish(material, "failed", code)
            else:
                material.update(status="unavailable", error_code=code, error_message=ERRORS[code], halted=True)
        elif not material["quota_released"]:
            material.update(error_code="cleanup_pending", error_message="结果已保存，送达或清理确认未完成；不会重复解析", halted=True)
        return await _save(db, op, stream, material)
