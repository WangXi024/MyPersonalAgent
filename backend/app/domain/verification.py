"""User-confirmed stdin/stdout sample verification, separate from AI review and mastery."""

import hashlib
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import defer

from app.db.models import (Attachment, Goal, Operation, Outbox, Submission, Task, TestContract,
                           VerificationCase, VerificationRun, now)
from app.db.session import SessionFactory
from app.domain.common import DomainError, digest, emit, lock_user, new_operation, owned, serialize, version
from app.domain.execution import operation_locked
from app.domain.sandbox_execution import _aware, storage_path
from app.integrations.settings import runtime_settings
from app.integrations.verification import MAX_OUTPUT, VerificationServiceError, VerificationTransport

ACTIVE = {"accepted", "running", "cancelling"}
FINAL = {"passed", "failed", "inconclusive", "cancelled", "superseded", "expired"}


def requirements_hash(task):
    return digest({"task_id": task.id, "plan_id": task.plan_id, "requirements_version": task.requirements_version,
                   "instructions": task.instructions, "criteria": task.criteria})


def contract_hash(task_id, requirements, comparator, cases):
    return digest({"schema": "verification-contract-v1", "task_id": task_id, "requirements_hash": requirements,
                   "comparator": comparator, "cases": cases, "timeout_seconds": 10, "memory_mb": 256})


def public_contract(item):
    return {**serialize(item, {"user_id", "idempotency_key", "request_hash"}), "status": "confirmed"}


async def confirm_contract(user_id, task_id, payload, key):
    from app.api.verification import ContractInput
    from pydantic import ValidationError
    try:
        payload = ContractInput.model_validate(payload).model_dump()
    except ValidationError:
        raise DomainError("invalid_test_contract", "请明确确认完整且有界的测试用例", 422) from None
    if payload["confirm"] is not True:
        raise DomainError("confirmation_required", "请先检查并明确确认测试用例", 422)
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        if not user.active:
            raise DomainError("unauthenticated", "账号已停用", 401)
        task = await owned(db, Task, task_id, user_id)
        goal = await owned(db, Goal, task.goal_id, user_id)
        fingerprint = digest({"task_id": task_id, **payload})
        prior = await db.scalar(select(TestContract).where(TestContract.user_id == user_id, TestContract.idempotency_key == key))
        if prior:
            if prior.request_hash != fingerprint:
                raise DomainError("idempotency_conflict", "该请求标识已用于不同测试用例")
            return public_contract(prior)
        version(task.state_version, payload["expected_version"])
        version(goal.control_revision, payload["expected_goal_revision"])
        version(task.requirements_version, payload["requirements_version"])
        if goal.status != "active" or task.plan_id != goal.active_plan_id or task.status in {"cancelled", "skipped"}:
            raise DomainError("test_scope_closed", "只能为当前有效计划中的任务确认测试用例")
        latest = await db.scalar(select(func.max(TestContract.version)).where(TestContract.task_id == task.id))
        required = requirements_hash(task)
        item = TestContract(user_id=user_id, goal_id=goal.id, task_id=task.id, version=(latest or 0) + 1,
            requirements_version=task.requirements_version, requirements_hash=required,
            contract_hash=contract_hash(task.id, required, payload["comparator"], payload["cases"]),
            comparator=payload["comparator"], cases=payload["cases"], idempotency_key=key, request_hash=fingerprint)
        db.add(item)
        await db.flush()
        await emit(db, stream, "test_contract.updated", item.id, "样例测试已明确确认；原规则保留为历史", goal.id)
        return public_contract(item)


async def list_contracts(user_id, task_id):
    async with SessionFactory() as db:
        await owned(db, Task, task_id, user_id)
        rows = await db.scalars(select(TestContract).where(TestContract.user_id == user_id, TestContract.task_id == task_id)
                               .order_by(TestContract.version.desc()).limit(50))
        return {"items": [public_contract(item) for item in rows]}


def _public_case(item):
    fields = {"id", "index", "name", "status", "reason", "exit_code", "output_complete", "output_truncated",
              "utf8_valid", "timed_out", "matched", "workspace_created", "cleanup_done"}
    return {**{key: getattr(item, key) for key in fields}, "stdout": item.stdout.decode("utf-8", "replace"),
            "stderr": item.stderr.decode("utf-8", "replace")}


async def public_run(db, item):
    value = serialize(item, {"user_id", "source", "plan_id", "cancel_requested", "deadline_at"})
    cases = await db.scalars(select(VerificationCase).where(VerificationCase.run_id == item.id,
        VerificationCase.user_id == item.user_id).order_by(VerificationCase.index))
    return {**value, "evidence_type": "program_verified", "cases": [_public_case(case) for case in cases]}


async def get_run(user_id, run_id):
    async with SessionFactory() as db:
        item = await db.scalar(select(VerificationRun).options(defer(VerificationRun.source)).where(
            VerificationRun.id == run_id, VerificationRun.user_id == user_id))
        if item is None:
            raise DomainError("not_found", "样例验证不存在", 404)
        return await public_run(db, item)


async def goal_runs(user_id, goal_id):
    async with SessionFactory() as db:
        await owned(db, Goal, goal_id, user_id)
        contracts = list(await db.scalars(select(TestContract).where(TestContract.user_id == user_id,
            TestContract.goal_id == goal_id).order_by(TestContract.created_at.desc()).limit(50)))
        runs = await db.scalars(select(VerificationRun).options(defer(VerificationRun.source)).where(VerificationRun.user_id == user_id,
            VerificationRun.goal_id == goal_id).order_by(VerificationRun.created_at.desc()).limit(30))
        return {"contracts": [public_contract(item) for item in contracts], "runs": [await public_run(db, item) for item in runs]}


async def _source(db, user_id, submission, attachment_id):
    if attachment_id:
        if attachment_id not in submission.attachment_ids:
            raise DomainError("invalid_attachment", "附件不属于已保存的提交", 422)
        attachment = await owned(db, Attachment, attachment_id, user_id)
        if attachment.status != "stored" or not attachment.filename.lower().endswith(".py") or attachment.size > 1024 * 1024:
            raise DomainError("invalid_attachment", "请选择不超过1 MiB的Python附件", 422)
        try:
            with storage_path(attachment.storage_key).open("rb") as handle:
                source = handle.read(1024 * 1024 + 1)
        except OSError:
            raise DomainError("attachment_unavailable", "已保存的附件暂不可用", 503) from None
        if len(source) != attachment.size or hashlib.sha256(source).hexdigest() != attachment.sha256:
            raise DomainError("attachment_changed", "已保存附件的完整性校验失败", 422)
    else:
        try:
            source = submission.text.encode("utf-8")
        except UnicodeError:
            raise DomainError("invalid_source", "Python代码必须为UTF-8文本", 422) from None
    try:
        source.decode("utf-8")
    except UnicodeError:
        raise DomainError("invalid_source", "Python代码必须为UTF-8文本", 422) from None
    if not source.strip() or len(source) > 1024 * 1024:
        raise DomainError("invalid_source", "请先保存不超过1 MiB的单文件Python代码", 422)
    return source


async def create_run(user_id, submission_id, payload, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        submission = await owned(db, Submission, submission_id, user_id)
        task = await owned(db, Task, submission.task_id, user_id)
        goal = await owned(db, Goal, task.goal_id, user_id)
        op, created = await new_operation(db, user, stream, key, "verification_run", {**payload, "submission_id": submission_id}, goal)
        if not created:
            return await public_run(db, await db.scalar(select(VerificationRun).options(defer(VerificationRun.source)).where(VerificationRun.operation_id == op.id)))
        if not user.active or not runtime_settings().verification_enabled:
            raise DomainError("verification_disabled", "样例验证尚未启用或账号不可用", 503)
        version(task.state_version, payload["expected_version"])
        version(goal.control_revision, payload["expected_goal_revision"])
        contract = await owned(db, TestContract, payload["contract_id"], user_id)
        latest = await db.scalar(select(func.max(TestContract.version)).where(TestContract.task_id == task.id))
        if (goal.status != "active" or goal.active_plan_id != task.plan_id or task.active_submission_id != submission.id
                or task.status in {"cancelled", "skipped"} or submission.requirements_version != task.requirements_version
                or contract.task_id != task.id or contract.requirements_version != task.requirements_version
                or contract.version != latest or contract.requirements_hash != requirements_hash(task)
                or contract.contract_hash != payload["expected_contract_hash"]
                or contract.contract_hash != contract_hash(task.id, contract.requirements_hash, contract.comparator, contract.cases)):
            raise DomainError("verification_scope_changed", "提交、任务或已确认的测试规则已变化，请刷新后重新选择")
        inflight = await db.scalar(select(func.count()).select_from(VerificationRun).where(VerificationRun.user_id == user_id,
            or_(VerificationRun.status.in_(ACTIVE), VerificationRun.cleanup_done.is_(False))))
        if inflight:
            raise DomainError("verification_busy", "已有样例验证正在运行或清理，请稍后重试", 429)
        count = await db.scalar(select(func.count()).select_from(VerificationRun).where(VerificationRun.user_id == user_id,
            VerificationRun.created_at >= now() - timedelta(hours=24)))
        if count >= 5:
            raise DomainError("verification_budget_exceeded", "最近24小时的五次样例验证额度已用完", 429)
        source = await _source(db, user_id, submission, payload.get("attachment_id"))
        item = VerificationRun(user_id=user_id, goal_id=goal.id, task_id=task.id, submission_id=submission.id,
            operation_id=op.id, attachment_id=payload.get("attachment_id"), contract_id=contract.id, contract_hash=contract.contract_hash,
            source_sha256=hashlib.sha256(source).hexdigest(), source=source, plan_id=task.plan_id,
            goal_revision=goal.control_revision, task_state_version=task.state_version, requirements_version=task.requirements_version,
            deadline_at=now() + timedelta(seconds=600))
        db.add(item)
        await db.flush()
        for index, sample in enumerate(contract.cases, 1):
            workspace_id = f"verify_{op.id.replace('-', '')}_{index}"
            db.add(VerificationCase(user_id=user_id, run_id=item.id, index=index, name=sample["name"], workspace_id=workspace_id,
                command_id=digest({"workspace": workspace_id, "source": item.source_sha256, "contract": contract.contract_hash})))
        db.add(Outbox(user_id=user_id, goal_id=goal.id, operation_id=op.id, kind="temporal", dedupe_key=f"verification:{op.id}",
            payload={"type": "verification_run", "user_id": user_id, "goal_id": goal.id, "operation_id": op.id, "run_id": item.id}))
        op.result = {"resource_id": item.id, "verification_run_id": item.id}
        await emit(db, stream, "verification.updated", item.id, "已排队验证用户确认的样例", goal.id, op.id)
        return await public_run(db, item)


async def _locked(db, operation_id):
    op, user, stream, goal = await operation_locked(db, operation_id)
    item = await db.scalar(select(VerificationRun).options(defer(VerificationRun.source)).where(VerificationRun.operation_id == op.id,
        VerificationRun.user_id == user.id).with_for_update())
    if not item or op.kind != "verification_run" or not goal or goal.id != item.goal_id:
        raise DomainError("not_found", "样例验证不存在", 404)
    cases = list(await db.scalars(select(VerificationCase).where(VerificationCase.run_id == item.id,
        VerificationCase.user_id == user.id).order_by(VerificationCase.index).with_for_update()))
    return item, cases, op, user, stream, goal


def _state(item):
    return {"run_id": item.id, "operation_id": item.operation_id, "status": item.status, "stage": item.stage,
        "terminal": item.status in FINAL and item.cleanup_done, "cleanup_done": item.cleanup_done,
        "cancel_requested": item.cancel_requested, "remaining_seconds": max(0, (_aware(item.deadline_at) - now()).total_seconds())}


async def _check(db, item, op, user, goal):
    if item.status in FINAL:
        return False
    # Finished runs only need lightweight cleanup metadata. Active checks explicitly
    # load the one bounded immutable source inside this Activity, never a list of blobs.
    await db.refresh(item, attribute_names=["source"])
    task = await db.get(Task, item.task_id)
    submission = await db.get(Submission, item.submission_id)
    contract = await db.get(TestContract, item.contract_id)
    latest = await db.scalar(select(func.max(TestContract.version)).where(TestContract.task_id == item.task_id))
    valid = (user.active and runtime_settings().verification_enabled and goal.status == "active"
        and goal.control_revision == item.goal_revision and goal.active_plan_id == item.plan_id
        and task and task.goal_id == goal.id and task.plan_id == item.plan_id and task.state_version == item.task_state_version
        and task.requirements_version == item.requirements_version and task.active_submission_id == item.submission_id
        and task.status not in {"cancelled", "skipped"} and submission and submission.user_id == user.id
        and submission.requirements_version == item.requirements_version and contract and contract.user_id == user.id
        and contract.task_id == task.id and contract.version == latest and contract.requirements_hash == requirements_hash(task)
        and contract.contract_hash == item.contract_hash
        and contract.contract_hash == contract_hash(task.id, contract.requirements_hash, contract.comparator, contract.cases)
        and hashlib.sha256(item.source).hexdigest() == item.source_sha256)
    if not valid:
        item.status, item.cancel_requested, item.error_message = "superseded", True, "目标、提交或测试规则已变化，停止本次验证"
    elif _state(item)["remaining_seconds"] <= 0:
        item.status, item.cancel_requested, item.error_message = "expired", True, "本次验证已达到十分钟时限"
    elif op.status in {"cancelled", "superseded"}:
        item.cancel_requested, item.status = True, "cancelling"
    return valid and item.status in ACTIVE and not item.cancel_requested


async def _changed(db, item, cases, op, stream):
    if cases and all(case.result_saved for case in cases) and item.status not in FINAL:
        item.status = ("cancelled" if item.cancel_requested else "inconclusive"
            if any(case.status in {"inconclusive", "cancelled"} for case in cases) else "failed"
            if any(case.status == "failed" for case in cases) else "passed")
    item.cleanup_done = bool(cases) and all(case.cleanup_done for case in cases)
    item.tests_verified = bool(cases) and item.status in {"passed", "failed"} and all(
        case.result_saved and case.status in {"passed", "failed"} and case.output_complete and not case.output_truncated
        and case.utf8_valid is True and case.exit_code is not None for case in cases)
    item.stage = "finished" if item.status in FINAL and item.cleanup_done else "cleaning" if item.status in FINAL else "running"
    item.updated_at = op.updated_at = now()
    op.status = "succeeded" if item.status in {"passed", "failed"} else "cancelled" if item.status == "cancelled" else "superseded" if item.status == "superseded" else "failed" if item.status in FINAL else "running"
    op.stage, op.error_message = item.stage, item.error_message
    op.result = {"resource_id": item.id, "verification_run_id": item.id, "status": item.status, "tests_verified": item.tests_verified}
    await emit(db, stream, "verification.updated", item.id, "样例验证记录已更新；不会自动修改学习评估", item.goal_id, op.id)


async def prepare_verification(operation_id):
    async with SessionFactory() as db, db.begin():
        item, cases, op, user, stream, goal = await _locked(db, operation_id)
        allowed = await _check(db, item, op, user, goal)
        if allowed and item.status == "accepted":
            item.status = "running"
        await _changed(db, item, cases, op, stream)
        return _state(item)


async def transport_scope(operation_id, case_id, *, cleanup=False, action=None):
    async with SessionFactory() as db, db.begin():
        item, cases, op, user, stream, goal = await _locked(db, operation_id)
        case = next((case for case in cases if case.id == case_id), None)
        if not case or case.workspace_id != f"verify_{op.id.replace('-', '')}_{case.index}" or case.command_id != digest(
                {"workspace": case.workspace_id, "source": item.source_sha256, "contract": item.contract_hash}):
            raise DomainError("verification_scope_changed", "验证工作区归属无效", 403)
        allowed = await _check(db, item, op, user, goal)
        if not cleanup and (not allowed or case.result_saved or case.cleanup_done):
            raise DomainError("verification_scope_changed", "样例验证授权已结束", 409)
        if (action == "Open" and not case.open_attempted
                or action == "Filesystem" and (not case.workspace_created or case.command_attempted)
                or action == "StartCommand" and (not case.seed_saved or not case.command_attempted)
                or action in {"PollCommand", "CancelCommand"} and not case.command_attempted):
            raise DomainError("verification_scope_changed", "验证请求尚未持久保存或已完成", 409)
        hashes = {"/workspace/source.py": item.source_sha256}
        if action == "Filesystem":
            contract = await db.get(TestContract, item.contract_id)
            hashes["/workspace/stdin.bin"] = hashlib.sha256(contract.cases[case.index - 1]["stdin"].encode()).hexdigest()
        return {"workspace_id": case.workspace_id, "command_id": case.command_id, "file_hashes": hashes}


def _inconclusive(case, reason, *, cancelled=False):
    if case.result_saved:
        return
    case.status, case.reason = ("cancelled" if cancelled else "inconclusive"), reason
    case.result_saved, case.finished_at = True, now()
    if not case.open_attempted:
        case.cleanup_done = True


def _receipt(item, case, contract, remote, *, cancel=False):
    if case.result_saved:
        return
    if (remote.command_id != case.command_id or remote.state not in {"starting", "running", "finished"}
            or len(remote.stdout) > MAX_OUTPUT or len(remote.stderr) > MAX_OUTPUT):
        raise VerificationServiceError("protocol_error")
    if remote.state != "finished" and not cancel:
        return
    case.stdout, case.stderr = bytes(remote.stdout), bytes(remote.stderr)
    case.exit_code = remote.exit_code if remote.exit_code_available and remote.state == "finished" else None
    case.output_complete = remote.output_complete and remote.state == "finished"
    case.output_truncated, case.timed_out = remote.output_truncated, remote.timed_out
    try:
        case.stdout.decode("utf-8")
        case.stderr.decode("utf-8")
        case.utf8_valid = True
    except UnicodeError:
        case.utf8_valid = False
    case.status, case.reason = "inconclusive", "protocol_error"
    if cancel or item.cancel_requested:
        case.status, case.reason = "cancelled", "cancelled"
    elif remote.timed_out:
        case.status, case.reason = "failed", "timeout"
    elif remote.cancelled or remote.error_code:
        case.reason = "cancelled" if remote.cancelled else "protocol_error"
    elif not case.utf8_valid:
        case.reason = "invalid_utf8"
    elif remote.output_truncated:
        case.reason = "output_truncated"
    elif not case.output_complete:
        case.reason = "output_incomplete"
    elif case.exit_code is None:
        case.reason = "missing_exit_code"
    elif case.exit_code != 0:
        case.status, case.reason = "failed", "nonzero_exit"
    else:
        actual, expected = case.stdout, contract.cases[case.index - 1]["expected_stdout"].encode("utf-8")
        if contract.comparator == "normalize_newlines":
            actual = actual.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            expected = expected.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        case.matched = actual == expected
        case.status, case.reason = ("passed", "matched") if case.matched else ("failed", "output_mismatch")
    case.result_saved, case.finished_at = True, now()


async def advance_verification(operation_id):
    """One durable step; uncertain Open/Start are reconciled, never issued again."""
    async with SessionFactory() as db, db.begin():
        item, cases, op, user, stream, goal = await _locked(db, operation_id)
        await _check(db, item, op, user, goal)
        stopping = item.status in FINAL or item.cancel_requested
        if stopping:
            for case in cases:
                if not case.result_saved and not case.open_attempted:
                    _inconclusive(case, "scope_changed" if item.status == "superseded" else "deadline"
                                  if item.status == "expired" else "cancelled", cancelled=True)
        current = next((case for case in cases if not case.result_saved or not case.cleanup_done), None)
        if current is None:
            await _changed(db, item, cases, op, stream)
            return _state(item)
        mode = "close" if current.result_saved else "cancel" if stopping and current.command_attempted else "abandon" if stopping else (
            "open" if not current.open_attempted else "status" if not current.workspace_created else "seed"
            if not current.seed_saved else "start" if not current.command_attempted else "poll")
        if mode == "open":
            current.open_attempted = True  # Commits before the RPC, including uncertain delivery.
            current.status, current.reason = "running", "opening"
        if mode == "start":
            current.command_attempted = True  # A retry uses Poll, never a second Start.
            current.status, current.reason = "running", "running"
        if mode == "abandon":
            _inconclusive(current, "scope_changed" if item.status == "superseded" else "cancelled", cancelled=True)
            mode = "close"
        case_id = current.id
        source = item.source if mode == "seed" else None
        contract = await db.get(TestContract, item.contract_id)
        stdin = contract.cases[current.index - 1]["stdin"].encode("utf-8") if mode == "seed" else None
        await _changed(db, item, cases, op, stream)
    transport = VerificationTransport(operation_id, case_id)
    remote = None
    error = None
    try:
        if mode == "seed":
            await transport.write("/workspace/source.py", source)
            await transport.write("/workspace/stdin.bin", stdin)
        else:
            remote = await getattr(transport, mode)()
    except VerificationServiceError as problem:
        error = problem.code
    except DomainError:
        error = "scope_changed"
    async with SessionFactory() as db, db.begin():
        item, cases, op, user, stream, goal = await _locked(db, operation_id)
        await _check(db, item, op, user, goal)
        current = next(case for case in cases if case.id == case_id)
        if error:
            if mode == "close":
                item.error_message = "工作区清理尚未确认，将继续重试"
            else:
                _inconclusive(current, error, cancelled=item.cancel_requested)
        elif mode in {"open", "status"}:
            if remote.state == "open":
                current.workspace_created = True
            elif remote.state in {"closed", "failed", "closing"}:
                _inconclusive(current, "workspace_unavailable")
        elif mode == "seed":
            current.seed_saved = True
        elif mode in {"start", "poll", "cancel"}:
            contract = await db.get(TestContract, item.contract_id)
            try:
                _receipt(item, current, contract, remote, cancel=mode == "cancel")
            except VerificationServiceError as problem:
                _inconclusive(current, problem.code)
        elif mode == "close":
            # A false cleanup acknowledgement is never converted into success.
            current.cleanup_done = remote.state == "closed" and remote.cleanup_done is True
        await _changed(db, item, cases, op, stream)
        return _state(item)


async def stop_verification(operation_id, *, expired=False):
    async with SessionFactory() as db, db.begin():
        item, cases, op, user, stream, goal = await _locked(db, operation_id)
        if item.status not in FINAL:
            item.cancel_requested, item.status = True, "expired" if expired else "cancelling"
            if expired:
                item.error_message = "本次验证已达到十分钟时限"
        await _changed(db, item, cases, op, stream)
        return _state(item)


async def fail_verification(operation_id):
    async with SessionFactory() as db, db.begin():
        item, cases, op, user, stream, goal = await _locked(db, operation_id)
        if item.status not in FINAL:
            item.status, item.error_message, item.cancel_requested = "inconclusive", "本次验证未取得完整可信回执", True
        for case in cases:
            _inconclusive(case, "service_unavailable")
        await _changed(db, item, cases, op, stream)
        return _state(item)


async def request_cancel(user_id, run_id, key):
    async with SessionFactory() as db, db.begin():
        user, stream = await lock_user(db, user_id)
        item = await owned(db, VerificationRun, run_id, user_id)
        goal = await owned(db, Goal, item.goal_id, user_id)
        command, created = await new_operation(db, user, stream, key, "verification_cancel", {"run_id": run_id}, goal)
        if created:
            command.status, command.stage, command.result = "succeeded", "saved", {"resource_id": item.id}
            if not item.cleanup_done or item.status in ACTIVE:
                if item.status not in FINAL:
                    item.cancel_requested, item.status = True, "cancelling"
                op = await db.get(Operation, item.operation_id)
                cases = list(await db.scalars(select(VerificationCase).where(VerificationCase.run_id == item.id)))
                await _changed(db, item, cases, op, stream)
                db.add(Outbox(user_id=user_id, goal_id=goal.id, operation_id=op.id, kind="temporal",
                    dedupe_key=f"verification-cancel:{item.id}:{command.id}", payload={"type": "verification_run_cancelled",
                        "user_id": user_id, "goal_id": goal.id, "operation_id": op.id, "run_id": item.id}))
        return await public_run(db, item)


async def applicable_evidence(db, user_id, goal_id):
    """At most 40 latest currently applicable receipts; no code, logs, files, or RPC."""
    latest_contract = select(func.max(TestContract.version)).where(TestContract.task_id == Task.id).correlate(Task).scalar_subquery()
    case_count = select(func.count()).select_from(VerificationCase).where(VerificationCase.run_id == VerificationRun.id).correlate(VerificationRun).scalar_subquery()
    invalid_case = select(VerificationCase.id).where(VerificationCase.run_id == VerificationRun.id, or_(
        VerificationCase.result_saved.is_(False), VerificationCase.output_complete.is_(False),
        VerificationCase.output_truncated.is_(True), VerificationCase.utf8_valid.is_not(True),
        VerificationCase.exit_code.is_(None), VerificationCase.status.notin_(("passed", "failed")))).correlate(VerificationRun).exists()
    rows = await db.execute(select(VerificationRun.id, VerificationRun.task_id, VerificationRun.submission_id,
        VerificationRun.contract_id, VerificationRun.contract_hash, VerificationRun.source_sha256,
        VerificationRun.status, VerificationRun.created_at, case_count.label("case_count"), TestContract.requirements_hash, Task)
        .join(Task, VerificationRun.task_id == Task.id).join(Goal, Task.goal_id == Goal.id)
        .join(Submission, VerificationRun.submission_id == Submission.id)
        .join(TestContract, VerificationRun.contract_id == TestContract.id)
        .where(VerificationRun.user_id == user_id, Goal.user_id == user_id, Goal.id == goal_id,
            VerificationRun.tests_verified.is_(True), VerificationRun.status.in_(("passed", "failed")),
            case_count > 0, ~invalid_case,
            VerificationRun.plan_id == Goal.active_plan_id, Task.plan_id == Goal.active_plan_id,
            Task.active_submission_id == Submission.id, Submission.user_id == user_id,
            VerificationRun.goal_revision == Goal.control_revision,
            VerificationRun.task_state_version == Task.state_version,
            VerificationRun.requirements_version == Task.requirements_version,
            Submission.requirements_version == Task.requirements_version, TestContract.user_id == user_id,
            TestContract.task_id == Task.id, TestContract.version == latest_contract,
            TestContract.contract_hash == VerificationRun.contract_hash,
            TestContract.requirements_version == Task.requirements_version,
            Task.status.notin_(("cancelled", "skipped")))
        .order_by(VerificationRun.created_at.desc()).limit(40))
    result = []
    for row in rows:
        if row.requirements_hash != requirements_hash(row.Task):
            continue
        result.append({"id": row.id, "task_id": row.task_id, "submission_id": row.submission_id,
            "contract_id": row.contract_id, "contract_hash": row.contract_hash, "source_sha256": row.source_sha256,
            "status": row.status, "passed": row.status == "passed", "tests_verified": True,
            "created_at": _aware(row.created_at).isoformat(), "case_count": row.case_count})
    return result
