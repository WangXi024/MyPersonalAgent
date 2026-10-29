"""Real database and authenticated download checks; no provider/model/network is used."""

from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select

from app.api import auth
from app.db.models import (
    CodingRun, Conversation, Evaluation, Goal, MemorySource, Message, Operation, Plan, SandboxExecution, Session,
    SkillProgress, Submission, Task, now,
)
from app.domain import reports
from app.domain.common import DomainError
from app.main import app


@pytest.fixture
def report_database(database, monkeypatch):
    monkeypatch.setattr(reports, "SessionFactory", database[0])
    return database


async def seed(database):
    factory, user, other = database
    async with factory() as db, db.begin():
        goal = Goal(user_id=user.id, title='学习中文\r\n<script>owned()</script>', outcome="完成函数练习",
                    criteria=["正确返回值"], status="active")
        foreign = Goal(user_id=other.id, title="FOREIGN_GOAL_SECRET", outcome="FOREIGN_OUTCOME_SECRET")
        unrelated = Goal(user_id=user.id, title="UNRELATED_GOAL_SECRET", outcome="OTHER_TARGET_SECRET")
        db.add_all([goal, foreign, unrelated])
        await db.flush()
        current = Plan(goal_id=goal.id, version=2, status="active", summary="已确认计划",
                       proposal={"private_internal": "PLAN_PROPOSAL_SECRET"}, availability_version=1)
        historical = Plan(goal_id=goal.id, version=1, status="superseded", summary="早期计划",
                          proposal={}, availability_version=1)
        db.add_all([current, historical])
        await db.flush()
        goal.active_plan_id = current.id
        task = Task(goal_id=goal.id, plan_id=current.id, title="CURRENT_TASK_MARKER", instructions="实现函数",
                    criteria=["返回值"], skills=["函数"], estimated_minutes=20, scheduled_date=date.today(),
                    status="completed", requirements_version=2)
        old_task = Task(goal_id=goal.id, plan_id=historical.id, title="HISTORICAL_TASK_MARKER", instructions="旧要求",
                        criteria=[], estimated_minutes=15, scheduled_date=date.today(), status="completed")
        db.add_all([task, old_task])
        await db.flush()
        submissions = []
        for target, version, text in (
            (task, 2, "CURRENT_SUBMISSION_MARKER\nreturn 7"),
            (old_task, 1, "HISTORICAL_SUBMISSION_MARKER"),
            (task, 1, "STALE_REQUIREMENTS_MARKER"),
        ):
            submission = Submission(user_id=user.id, task_id=target.id, text=text, content_hash=uuid4().hex,
                                    requirements_version=version, requirements_snapshot={})
            db.add(submission)
            await db.flush()
            submissions.append(submission)
        task.active_submission_id = submissions[0].id
        old_task.active_submission_id = submissions[1].id
        # Malformed cross-owner row must be filtered even when task/goal relationships look owned.
        db.add(Submission(user_id=other.id, task_id=task.id, text="CROSS_OWNER_SUBMISSION_SECRET",
                          content_hash=uuid4().hex, requirements_version=2, requirements_snapshot={}))
        evaluations = []
        for index, submission in enumerate(submissions):
            op = Operation(user_id=user.id, goal_id=goal.id, kind="assessment", idempotency_key=uuid4().hex,
                           request_hash=uuid4().hex, status="succeeded",
                           payload={"secret": "OPERATION_PAYLOAD_SECRET"},
                           agent_history=[{"secret": "AGENT_INTERNAL_SECRET"}])
            db.add(op)
            await db.flush()
            evaluation = Evaluation(submission_id=submission.id, operation_id=op.id, passed=True,
                feedback=("CURRENT_FEEDBACK_MARKER", "HISTORICAL_FEEDBACK_MARKER", "STALE_FEEDBACK_MARKER")[index],
                evidence_type="ai_review", rubric_version=submission.requirements_version, valid=True,
                details={"private": "EVALUATION_DETAILS_SECRET"})
            db.add(evaluation)
            await db.flush()
            evaluations.append(evaluation)
        db.add(SkillProgress(user_id=user.id, goal_id=goal.id, name="历史技能",
                            status="evidence_available", evaluation_id=evaluations[1].id, review_at=now()))
        other_op = Operation(user_id=other.id, goal_id=foreign.id, kind="assessment",
                             idempotency_key=uuid4().hex, request_hash=uuid4().hex)
        db.add(other_op)
        await db.flush()
        db.add(Evaluation(submission_id=submissions[0].id, operation_id=other_op.id, passed=True,
            feedback="CROSS_OWNER_EVALUATION_SECRET", evidence_type="program_verified",
            rubric_version=2, valid=True, details={}))
        conversation = Conversation(user_id=user.id, goal_id=goal.id)
        db.add(conversation)
        await db.flush()
        db.add(Message(conversation_id=conversation.id, seq=1, role="assistant", type="assistant_reply",
                       content="PRIVATE_CHAT_SECRET", control_revision=1))
        db.add(MemorySource(user_id=user.id, source_id=goal.id, source_type="manual", content="PRIVATE_MEMORY_SECRET"))
    return goal, task, old_task, submissions, evaluations, foreign


def section(content, marker):
    start = content.index(marker)
    next_heading = content.find("\n### ", start + len(marker))
    return content[start:next_heading if next_heading != -1 else None]


async def test_report_owned_snapshot_current_and_historical_evidence(report_database):
    _, user, _ = report_database
    goal, task, old_task, submissions, evaluations, _ = await seed(report_database)
    report = await reports.render_learning_report(user.id, goal.id)
    text = report.content.decode("utf-8")
    assert report.filename == f"learning-report-{goal.id}.md"
    assert "当前计划：共 1 项" in text and "历史计划：共 1 项" in text
    assert "当前计划" in section(text, f"### 任务 {reports._inline(task.id)}")
    assert "历史计划（不代表当前进度）" in section(text, f"### 任务 {reports._inline(old_task.id)}")
    assert "当前提交 / 当前要求" in section(text, f"### 提交 {reports._inline(submissions[0].id)}")
    assert "历史提交（不代表当前进度）" in section(text, f"### 提交 {reports._inline(submissions[2].id)}")
    assert "当前适用（版本匹配）" in section(text, f"### 评估 {reports._inline(evaluations[0].id)}")
    for evaluation in evaluations[1:]:
        assert "历史证据（不代表当前进度）" in section(text, f"### 评估 {reports._inline(evaluation.id)}")
    assert "历史证据（不代表当前进度）" in text.split("## 技能索引与复习日期")[1]
    assert "ai\\_review" in text and "AI 审阅" in text and "不能据此认定已验证掌握" in text
    for secret in ("FOREIGN_GOAL_SECRET", "UNRELATED_GOAL_SECRET", "CROSS_OWNER_SUBMISSION_SECRET",
                   "CROSS_OWNER_EVALUATION_SECRET", "PRIVATE_CHAT_SECRET", "PRIVATE_MEMORY_SECRET",
                   "OPERATION_PAYLOAD_SECRET", "AGENT_INTERNAL_SECRET", "EVALUATION_DETAILS_SECRET",
                   "PLAN_PROPOSAL_SECRET"):
        assert secret not in text


@pytest.mark.parametrize("change", ["invalid", "superseded_submission", "rubric", "requirements"])
async def test_previously_current_evidence_is_reclassified_at_export(report_database, change):
    factory, user, _ = report_database
    goal, task, _, submissions, evaluations, _ = await seed(report_database)
    async with factory() as db, db.begin():
        if change == "invalid":
            (await db.get(Evaluation, evaluations[0].id)).valid = False
        elif change == "rubric":
            (await db.get(Evaluation, evaluations[0].id)).rubric_version = 1
        elif change == "requirements":
            (await db.get(Task, task.id)).requirements_version = 3
        else:
            (await db.get(Task, task.id)).active_submission_id = submissions[2].id
    text = (await reports.render_learning_report(user.id, goal.id)).content.decode()
    assert "历史证据（不代表当前进度）" in section(text, f"### 评估 {reports._inline(evaluations[0].id)}")


async def test_report_absent_foreign_and_bad_identifier_same_not_found(report_database):
    _, user, other = report_database
    goal, *_, foreign = await seed(report_database)
    for owner, identifier in ((other.id, goal.id), (user.id, foreign.id), (user.id, str(uuid4())),
                              (user.id, '../../report\r\nInjected: true')):
        with pytest.raises(DomainError) as caught:
            await reports.render_learning_report(owner, identifier)
        assert caught.value.status == 404 and caught.value.code == "not_found"


async def test_report_only_selects_and_never_reads_private_tables(report_database):
    factory, user, _ = report_database
    goal, *_ = await seed(report_database)
    statements = []
    async with factory() as db:
        engine = db.bind.sync_engine
    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", observe)
    try:
        await reports.render_learning_report(user.id, goal.id)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert statements and all(statement.lstrip().startswith("select") for statement in statements)
    forbidden = ("messages", "memory_sources", "voice_turns", "voice_configs", "model_configs", "attachments")
    assert not any(f"from {table}" in statement or f"join {table}" in statement
                   for table in forbidden for statement in statements)
    assert not any("agent_history" in statement or "stdout" in statement or "proposal" in statement
                   for statement in statements)


async def test_download_real_auth_utf8_headers_and_revoked_session(report_database):
    factory, user, _ = report_database
    goal, *_, foreign = await seed(report_database)
    token = "test-only-report-login-cookie"
    async with factory() as db, db.begin():
        login = Session(user_id=user.id, token_hash=auth.token_hash(token), csrf_hash=auth.token_hash("csrf"),
                        expires_at=now() + timedelta(hours=1))
        db.add(login)
    path = f"/api/v1/goals/{goal.id}/report.md"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080") as client:
        assert (await client.get(path)).status_code == 401
        client.cookies.set(auth.COOKIE, token)
        result = await client.get(path)
        assert result.status_code == 200, result.text
        assert result.headers["content-type"] == "text/markdown; charset=utf-8"
        assert result.headers["content-disposition"] == f'attachment; filename="learning-report-{goal.id}.md"'
        assert result.headers["cache-control"] == "private, no-store"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert "学习中文" in result.content.decode("utf-8")
        assert "\r\n<script>" not in result.headers["content-disposition"]
        for identifier in (foreign.id, str(uuid4())):
            assert (await client.get(f"/api/v1/goals/{identifier}/report.md")).status_code == 404
        async with factory() as db, db.begin():
            (await db.get(Session, login.id)).revoked = True
        assert (await client.get(path)).status_code == 401


async def test_empty_goal_and_cross_goal_plan_pointer_never_gain_current_scope(report_database):
    factory, user, _ = report_database
    goal, *_ = await seed(report_database)
    async with factory() as db, db.begin():
        empty = Goal(user_id=user.id, title="空目标", outcome="尚未开始")
        db.add(empty)
        await db.flush()
        empty.active_plan_id = (await db.scalar(select(Goal).where(Goal.id == goal.id))).active_plan_id
    text = (await reports.render_learning_report(user.id, empty.id)).content.decode()
    assert "尚无有效的当前计划" in text and "共 0 项" in text
    assert "CURRENT_TASK_MARKER" not in text and "CURRENT_FEEDBACK_MARKER" not in text


async def test_markdown_content_cannot_escape_quoted_blocks_or_inject_filename(report_database):
    factory, user, _ = report_database
    goal, task, *_ = await seed(report_database)
    payload = '\r\n# FORGED_HEADING\n```\n<script>alert(1)</script>\n![remote](https://example.invalid/x)\n\x01'
    async with factory() as db, db.begin():
        (await db.get(Task, task.id)).instructions = payload
    report = await reports.render_learning_report(user.id, goal.id)
    text = report.content.decode()
    assert "\n# FORGED_HEADING" not in text and "\n<script>" not in text and "\n![remote]" not in text
    assert "    # FORGED_HEADING" in text and "    <script>alert(1)</script>" in text
    assert "\x01" not in text and "\r" not in text
    assert report.filename == f"learning-report-{goal.id}.md"


def test_formatter_removes_nul_without_sending_it_to_the_database():
    assert reports._text("before\x00after") == "beforeafter"


async def test_query_row_limits_and_field_truncation_are_explicit(report_database):
    factory, user, _ = report_database
    goal, task, *_ = await seed(report_database)
    async with factory() as db, db.begin():
        current = await db.get(Task, task.id)
        current.instructions = "长" * 5000 + "END_SECRET_OUTSIDE_LIMIT"
        current.created_at = now() + timedelta(days=1)
        for index in range(55):
            db.add(Task(goal_id=goal.id, plan_id=current.plan_id, title=f"bounded task {index}", instructions="x",
                        criteria=[], estimated_minutes=5, scheduled_date=date.today()))
    text = (await reports.render_learning_report(user.id, goal.id)).content.decode()
    assert text.count("### 任务 ") == reports.CURRENT_TASK_LIMIT + 1  # one historical task
    assert "当前计划：共 56 项" in text
    assert "内容已截断" in text and "END_SECRET_OUTSIDE_LIMIT" not in text
    assert "未列出的记录不表示不存在" in text


def test_total_document_byte_ceiling_preserves_utf8_and_reports_omission():
    doc = reports._Document()
    for _ in range(1000):
        doc.add("学" * 1600)
    data = doc.finish()
    assert len(data) <= reports.MAX_REPORT_BYTES
    assert "后续记录已省略" in data.decode("utf-8")


async def test_execution_and_coder_receipts_are_not_mastery_or_code_output(report_database):
    factory, user, _ = report_database
    goal, task, _, submissions, _, _ = await seed(report_database)
    async with factory() as db, db.begin():
        operations = [Operation(user_id=user.id, goal_id=goal.id, kind=kind,
            idempotency_key=uuid4().hex, request_hash=uuid4().hex) for kind in ("sandbox_execution", "coding_run")]
        db.add_all(operations)
        await db.flush()
        execution = SandboxExecution(user_id=user.id, goal_id=goal.id, task_id=task.id,
            submission_id=submissions[0].id, operation_id=operations[0].id, request_id=uuid4().hex,
            request_hash=uuid4().hex, code="SOURCE_CODE_SECRET", limits={}, goal_revision=goal.control_revision,
            requirements_version=2, plan_id=task.plan_id, status="completed", outcome="success", result_saved=True,
            cleanup_done=True, stdout="EXECUTION_STDOUT_SECRET", stderr="EXECUTION_STDERR_SECRET")
        coding = CodingRun(user_id=user.id, goal_id=goal.id, task_id=task.id,
            submission_id=submissions[2].id, operation_id=operations[1].id, plan_id=task.plan_id,
            goal_revision=goal.control_revision, requirements_version=1, instruction="CODER_INSTRUCTION_SECRET",
            source="CODER_SOURCE_SECRET", workspace_id=uuid4().hex, status="succeeded", cleanup_done=True,
            summary="MODEL_SUMMARY_MASTERY_CLAIM_SECRET")
        db.add_all([execution, coding])
        await db.flush()
    text = (await reports.render_learning_report(user.id, goal.id)).content.decode()
    receipt = section(text, f"执行 {reports._inline(execution.id)}")
    assert "当前提交回执" in receipt and "result_saved=true" in receipt
    assert f"Coder {reports._inline(coding.id)}；历史回执" in text
    assert "tests_verified=false" in text and "执行回执也不证明测试通过或技能掌握" in text
    for secret in ("SOURCE_CODE_SECRET", "EXECUTION_STDOUT_SECRET", "EXECUTION_STDERR_SECRET",
                   "CODER_INSTRUCTION_SECRET", "CODER_SOURCE_SECRET", "MODEL_SUMMARY_MASTERY_CLAIM_SECRET"):
        assert secret not in text
