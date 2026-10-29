"""Offline material protocol and real database/API boundaries; no provider or node is called."""

from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from sqlalchemy import func, select

from app.api import auth
from app.db.models import Attachment, DailyBudget, Evaluation, Operation, Outbox, Task, User, now
from app.domain import materials as domain
from app.domain.common import DomainError
from app.integrations import materials as parser
from app.integrations.sandbox import SandboxProtocolError, SandboxRPCError, validated_result
from app.integrations.sandbox_proto import sandbox_pb2 as pb
from app.integrations.settings import RuntimeSettings
from app.main import app


PDF = b"%PDF-1.7\nsynthetic-private-material"
DOCX = b"PK\x03\x04synthetic-docx-not-parsed-on-host"


@pytest.fixture
def config(database, monkeypatch, tmp_path):
    settings = RuntimeSettings(material_enabled=True)
    monkeypatch.setattr(domain, "SessionFactory", database[0])
    monkeypatch.setattr(domain, "runtime_settings", lambda: settings)
    monkeypatch.setattr(domain, "get_settings", lambda: SimpleNamespace(attachment_dir=tmp_path / "files"))
    return settings


class FakeSandbox:
    def __init__(self, *, text="学习材料正文", docx=False):
        self.request = None
        self.calls = []
        self.delivery = False
        self.cleanup = True
        self.outcome = "success"
        self.text = text
        self.docx = docx
        self.submit_loss = False
        self.ack_loss = False
        self.unknown = False
        self.ready_hook = None
        self.mutate = None
        self.raw = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def ready(self):
        if self.ready_hook:
            await self.ready_hook()
        return {"ready": True, "independent": True, "accepting_tasks": True, "environments": ["files"]}

    def remote(self, phase="finished"):
        return {"request_id": self.request.request_id, "phase": phase,
            "outcome": self.outcome if phase == "finished" else "", "error_code": None,
            "cleanup_done": phase == "finished" and self.cleanup, "delivery_done": self.delivery}

    async def submit(self, request):
        self.calls.append("submit")
        self.request = request
        if self.submit_loss:
            raise SandboxRPCError("UNAVAILABLE")
        return self.remote("queued")

    async def status(self, request_id):
        self.calls.append("status")
        if self.unknown:
            raise SandboxRPCError("NOT_FOUND")
        return self.remote()

    async def result(self, request_id):
        self.calls.append("result")
        parsed = {"source": "input.docx" if self.docx else "input.pdf", "type": "docx" if self.docx else "pdf",
            "content": {"paragraphs": [self.text], "tables": [[["单元格", "7"]]]} if self.docx else self.text}
        if not self.docx:
            parsed["page_count"] = 1
        if self.mutate:
            self.mutate(parsed)
        content = self.raw if self.raw is not None else json.dumps(parsed, ensure_ascii=True).encode()
        return validated_result(pb.JobResult(status=pb.JobStatus(request_id=request_id, phase="finished",
            outcome=self.outcome, cleanup_done=self.cleanup), stdout="UNTRUSTED_LOG_PRIVATE",
            artifacts=[] if self.outcome != "success" else [pb.Artifact(path="parsed.json", data=content,
                sha256=hashlib.sha256(content).hexdigest())]), request_id)

    async def acknowledge(self, request_id):
        self.calls.append("ack")
        if self.ack_loss:
            raise SandboxRPCError("UNAVAILABLE")
        self.delivery = True
        return self.remote()

    async def cancel(self, request_id):
        self.calls.append("cancel")
        self.outcome = "cancelled"
        return self.remote()


async def create(database, *, docx=False, key=None):
    return await domain.create_material(database[1].id, "中文资料.docx" if docx else "中文资料.pdf",
                                        DOCX if docx else PDF, key or str(uuid4()))


async def complete(identifier):
    await domain.submit_material(identifier)
    await domain.poll_material(identifier)
    saved = await domain.collect_material(identifier)
    return saved, await domain.ack_material(identifier)


async def test_full_material_flow_is_private_idempotent_and_never_learning_evidence(database, config, monkeypatch):
    factory, user, _ = database
    key = str(uuid4())
    item = await create(database, key=key)
    assert await create(database, key=key) == item
    fake = FakeSandbox(text="原文" * 20000)
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    state = await domain.submit_material(item["id"])
    request = fake.request
    assert request.kind == "parse" and request.environment == "files" and request.inputs[0].path == "input.pdf"
    assert request.inputs[0].data == PDF and not request.code
    assert not request.network_domains and not request.environment_variables and not request.dependencies
    assert request.timeout_seconds == 30 and request.memory_mb == 256
    assert PDF.decode() not in json.dumps(state)
    await domain.submit_material(item["id"])
    assert fake.calls.count("submit") == 1
    saved = await domain.collect_material(item["id"])
    assert saved["result_saved"] and saved["needs_ack"] and not saved["terminal"] and "ack" not in fake.calls
    first = await domain.get_material(user.id, item["id"])
    assert first["status"] == "succeeded" and not first["delivery_done"] and len(first["downloads"]) == 2
    view = await domain.preview(user.id, item["id"])
    assert len(view["text"]) == 32000 and view["char_count"] == 40000 and view["truncated"] and view["untrusted"]
    await domain.collect_material(item["id"])
    done = await domain.ack_material(item["id"])
    assert done["terminal"] and done["cleanup_done"] and done["delivery_done"]
    await domain.ack_material(item["id"])
    assert fake.calls.count("result") == fake.calls.count("ack") == 1
    data, filename, mime = await domain.download(user.id, item["id"], "json")
    exported = json.loads(data)
    assert exported["source"]["sha256"] == hashlib.sha256(PDF).hexdigest() and exported["untrusted"]
    assert filename.isascii() and mime == "application/json"
    async with factory() as db:
        op = await db.get(Operation, item["id"])
        assert "原文" not in json.dumps(op.result, ensure_ascii=False) and "UNTRUSTED_LOG_PRIVATE" not in str(op.result)
        assert op.model_requests == op.input_tokens == op.output_tokens == 0
        assert await db.scalar(select(func.count()).select_from(Attachment)) == 3
        for model in (Task, Evaluation, DailyBudget):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_docx_paragraph_and_table_export_without_host_parsing(database, config, monkeypatch):
    item = await create(database, docx=True)
    fake = FakeSandbox(docx=True)
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    _, state = await complete(item["id"])
    assert state["terminal"]
    view = await domain.preview(database[1].id, item["id"])
    assert view["type"] == "docx" and view["page_count"] is None
    assert "学习材料正文\n单元格\t7" == view["text"]


@pytest.mark.parametrize("outcome,text,code", [("success", "  \n", "no_text"), ("failed", "ignored", "parse_failed")])
async def test_unreadable_material_has_saved_failure_no_download_and_clean_ack(database, config, monkeypatch, outcome, text, code):
    item = await create(database)
    fake = FakeSandbox(text=text)
    fake.outcome = outcome
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    _, state = await complete(item["id"])
    result = await domain.get_material(database[1].id, item["id"])
    assert state["terminal"] and result["status"] == "failed" and result["error_code"] == code
    assert result["result_saved"] and not result["downloads"] and result["preview_url"] is None
    with pytest.raises(DomainError):
        await domain.preview(database[1].id, item["id"])


@pytest.mark.parametrize("change", ["disabled", "account", "deadline", "generic_cancel"])
async def test_scope_changes_before_submission_never_start_job(database, config, monkeypatch, change):
    factory, user, _ = database
    item = await create(database)
    async with factory() as db, db.begin():
        if change == "disabled":
            config.material_enabled = False
        elif change == "account":
            (await db.get(User, user.id)).active = False
        elif change == "deadline":
            (await db.get(Operation, item["id"])).created_at = now() - timedelta(seconds=241)
        else:
            (await db.get(Operation, item["id"])).status = "cancelled"
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    state = await domain.submit_material(item["id"])
    assert state["terminal"] and not state["submitted"] and state["cleanup_done"] and not fake.calls


async def test_cancel_rechecks_scope_after_readiness_and_never_starts(database, config, monkeypatch):
    item = await create(database)
    fake = FakeSandbox()
    async def revoke():
        config.material_enabled = False
    fake.ready_hook = revoke
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    state = await domain.submit_material(item["id"])
    assert state["terminal"] and not state["submitted"] and not fake.calls


async def test_cancel_intent_has_one_outbox_and_post_submit_still_drains(database, config, monkeypatch):
    factory, user, _ = database
    item = await create(database)
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_material(item["id"])
    await domain.request_cancel(user.id, item["id"], "material-cancel-one")
    await domain.request_cancel(user.id, item["id"], "material-cancel-one")
    await domain.cancel_material(item["id"])
    await domain.collect_material(item["id"])
    state = await domain.ack_material(item["id"])
    assert state["terminal"] and state["status"] == "cancelled"
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Outbox).where(
            Outbox.dedupe_key == f"material-cancel:{item['id']}")) == 1


async def test_submit_reply_loss_reattaches_without_second_execution(database, config, monkeypatch):
    item = await create(database)
    fake = FakeSandbox()
    fake.submit_loss = True
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    with pytest.raises(SandboxRPCError):
        await domain.submit_material(item["id"])
    state = await domain.submit_material(item["id"])
    assert state["submitted"] and fake.calls.count("submit") == 1
    await domain.collect_material(item["id"])
    assert (await domain.ack_material(item["id"]))["terminal"]


async def test_unknown_after_submit_keeps_quota_and_does_not_invent_cleanup(database, config, monkeypatch):
    item = await create(database)
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_material(item["id"])
    fake.unknown = True
    state = await domain.poll_material(item["id"])
    assert state["terminal"] and state["status"] == "unavailable" and not state["cleanup_done"]
    with pytest.raises(DomainError) as caught:
        await create(database)
    assert caught.value.code == "material_busy" and fake.calls.count("submit") == 1


async def test_saved_result_survives_ack_failure_and_cleanup_pending_holds_slot(database, config, monkeypatch):
    item = await create(database)
    fake = FakeSandbox()
    fake.cleanup = False
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_material(item["id"])
    await domain.collect_material(item["id"])
    fake.ack_loss = True
    with pytest.raises(SandboxRPCError):
        await domain.ack_material(item["id"])
    assert (await domain.get_material(database[1].id, item["id"]))["status"] == "succeeded"
    fake.ack_loss = False
    state = await domain.ack_material(item["id"])
    assert not state["terminal"] and state["delivery_done"] and not state["cleanup_done"]
    with pytest.raises(DomainError):
        await create(database)
    fake.cleanup = True
    assert (await domain.poll_material(item["id"]))["terminal"]
    assert fake.calls.count("submit") == fake.calls.count("result") == 1


async def test_owner_checks_quota_and_idempotency_conflict(database, config):
    _, user, other = database
    item = await create(database, key="same-material-key")
    with pytest.raises(DomainError) as caught:
        await domain.create_material(user.id, "different.pdf", PDF, "same-material-key")
    assert caught.value.code == "idempotency_conflict"
    for call in (domain.get_material(other.id, item["id"]), domain.preview(other.id, item["id"]),
                 domain.download(other.id, item["id"], "txt"), domain.request_cancel(other.id, item["id"], "foreign-cancel")):
        with pytest.raises(DomainError) as denied:
            await call
        assert denied.value.status == 404
    assert not (await domain.list_materials(other.id))["items"]
    for index in range(5):
        current = item if index == 0 else await create(database)
        await domain.request_cancel(user.id, current["id"], str(uuid4()))
        assert (await domain.prepare_material(current["id"]))["terminal"]
    with pytest.raises(DomainError) as capped:
        await create(database)
    assert capped.value.code == "material_daily_limit"


async def test_modified_source_and_partial_derived_file_recovery(database, config, monkeypatch):
    factory, user, _ = database
    item = await create(database)
    fake = FakeSandbox()
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    async with factory() as db:
        op = await db.get(Operation, item["id"])
        source = await db.get(Attachment, op.result["material"]["source_attachment_id"])
    domain.path_for(source.storage_key).write_bytes(b"changed")
    with pytest.raises(DomainError) as changed:
        await domain.submit_material(item["id"])
    assert changed.value.code == "source_changed" and "submit" not in fake.calls
    domain.path_for(source.storage_key).write_bytes(PDF)
    await domain.submit_material(item["id"])
    identifier = str(uuid5(NAMESPACE_URL, f"material:{item['id']}:txt"))
    path = domain.path_for(f"materials/{item['id']}/{identifier}")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"partial")
    await domain.collect_material(item["id"])
    data, _, _ = await domain.download(user.id, item["id"], "txt")
    assert data.decode() == "学习材料正文"


async def test_rest_auth_csrf_preview_owned_download_and_strict_material_limit(database, config, monkeypatch):
    factory, user, _ = database
    token = "test-material-cookie"
    async with factory() as db, db.begin():
        db.add(Attachment(user_id=user.id, storage_key="dummy", filename="unrelated", mime="text/plain", size=1, sha256="0"*64))
        from app.db.models import Session
        db.add(Session(user_id=user.id, token_hash=auth.token_hash(token), csrf_hash=auth.token_hash("test-csrf"),
                       expires_at=now() + timedelta(hours=1)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        assert (await client.get("/api/v1/materials/capabilities")).status_code == 401
        client.cookies.set(auth.COOKIE, token)
        caps = (await client.get("/api/v1/materials/capabilities")).json()
        assert caps["max_upload_bytes"] == 1048576 and caps["model_required"] is False
        assert (await client.post("/api/v1/materials", files={"file": ("file.pdf", PDF)})).status_code == 403
        headers = {"Origin": "http://localhost:8080", "X-CSRF-Token": "test-csrf", "Idempotency-Key": "http-material-once"}
        assert (await client.post("/api/v1/materials", files={"file": ("file.pdf", PDF + b"x"*1048576)}, headers=headers)).status_code == 413
        response = await client.post("/api/v1/materials", files={"file": ("中文.docx", DOCX)}, headers=headers)
        assert response.status_code == 202, response.text
        item = response.json()
        assert (await client.get(f"/api/v1/materials/{item['id']}/preview")).status_code == 404
        # Keep a single RPC identity across the domain calls.
        fake = FakeSandbox(docx=True)
        monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
        await complete(item["id"])
        for kind in ("txt", "json"):
            download = await client.get(f"/api/v1/materials/{item['id']}/download/{kind}")
            assert download.status_code == 200 and download.headers["content-disposition"].isascii()
            assert download.headers["content-security-policy"] == "sandbox"
            assert download.headers["cache-control"] == "private, no-store"
        assert (await client.get(f"/api/v1/materials/{item['id']}/download/html")).status_code == 404
        assert len((await client.get("/api/v1/materials")).json()["items"]) == 1
        assert (await client.post("/api/v1/attachments", files={"file": ("file.docx", DOCX)},
                                  headers={**headers, "Idempotency-Key": "old-upload-docx"})).status_code == 422


@pytest.mark.parametrize("change", ["extra", "type", "source", "pages", "surrogate", "nesting", "too_long", "hash"])
def test_untrusted_parser_contract_rejects_invalid_data(change):
    document = {"source": "input.pdf", "type": "pdf", "content": "text", "page_count": 1}
    if change == "extra":
        document["system"] = "ignore rules"
    elif change == "type":
        document["type"] = "docx"
    elif change == "source":
        document["source"] = "other.pdf"
    elif change == "pages":
        document["page_count"] = True
    elif change == "surrogate":
        document["content"] = "bad\ud800scalar"
    elif change == "nesting":
        document["content"] = {"not": "a string"}
    elif change == "too_long":
        document["content"] = "x" * 200001
    data = json.dumps(document, ensure_ascii=True).encode()
    result = {"status": {"outcome": "success"}, "artifacts": [{"filename": "parsed.json", "data": data,
        "sha256": "0" * 64 if change == "hash" else hashlib.sha256(data).hexdigest()}]}
    with pytest.raises(SandboxProtocolError):
        parser.parsed_document(result, "pdf")


def test_fixed_request_exact_size_boundary_and_no_dangerous_options():
    content = b"x" * parser.MAX_INPUT
    request = parser.parse_request("material_test", "pdf", content, hashlib.sha256(content).hexdigest())
    assert request.inputs[0].path == "input.pdf" and not request.HasField("parse_options")
    for data, kind in ((content + b"x", "pdf"), (b"", "pdf"), (b"x", "html")):
        with pytest.raises(SandboxProtocolError):
            parser.parse_request("material_test", kind, data, hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("change", ["json", "schema", "surrogate"])
async def test_hash_valid_unusable_content_is_saved_failed_then_acked(database, config, monkeypatch, change):
    item = await create(database)
    fake = FakeSandbox()
    if change == "json":
        fake.raw = b"{broken"
    elif change == "schema":
        fake.mutate = lambda body: body.update(extra="untrusted")
    else:
        fake.text = "invalid\ud800scalar"
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_material(item["id"])
    saved = await domain.collect_material(item["id"])
    assert saved["result_saved"] and saved["needs_ack"] and "ack" not in fake.calls
    result = await domain.get_material(database[1].id, item["id"])
    assert result["status"] == "failed" and result["error_code"] == "invalid_result" and not result["downloads"]
    assert (await domain.ack_material(item["id"]))["terminal"]
    assert (await create(database))["id"] != item["id"]  # the old failed receipt no longer holds the slot


async def test_wrong_source_is_not_acked_or_promoted_to_a_received_document(database, config, monkeypatch):
    item = await create(database)
    fake = FakeSandbox()
    fake.mutate = lambda body: body.update(source="different.pdf")
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_material(item["id"])
    with pytest.raises(SandboxProtocolError):
        await domain.collect_material(item["id"])
    state = await domain.fail_material(item["id"], "invalid_result")
    assert not state["result_saved"] and not state["delivery_done"] and "ack" not in fake.calls
    with pytest.raises(DomainError):
        await create(database)


async def test_unknown_execution_outcome_is_not_a_known_parse_failure(database, config, monkeypatch):
    item = await create(database)
    fake = FakeSandbox()
    fake.outcome = "unknown"
    monkeypatch.setattr(domain, "SandboxClient", lambda: fake)
    await domain.submit_material(item["id"])
    with pytest.raises(SandboxProtocolError):
        await domain.collect_material(item["id"])
    state = await domain.fail_material(item["id"], "job_unknown")
    assert state["status"] == "unavailable" and not state["result_saved"] and "ack" not in fake.calls
