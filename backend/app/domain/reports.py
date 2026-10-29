"""Bounded, read-only learning reports from owned database records, without a model."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import re
from uuid import UUID

from sqlalchemy import case, func, select

from app.db.models import (
    CodingRun, Evaluation, Goal, Operation, Plan, SandboxExecution, SkillProgress,
    Submission, Task, now,
)
from app.db.session import SessionFactory
from app.domain.common import DomainError


MAX_REPORT_BYTES = 512 * 1024
CURRENT_TASK_LIMIT = 40
HISTORICAL_TASK_LIMIT = 20
RECORD_LIMIT = 40
TEXT_LIMIT = 1600
TRUNCATED = "…〔内容已截断〕"


@dataclass(frozen=True)
class LearningReport:
    filename: str
    content: bytes


def _text(value, limit=TEXT_LIMIT):
    if value is None:
        return "未记录"
    value = str(value).encode("utf-8", errors="replace").decode("utf-8")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(char for char in value if char in "\n\t" or ord(char) >= 32)
    return value[:limit] + (TRUNCATED if len(value) > limit else "")


def _inline(value, limit=200):
    value = html.escape(_text(value, limit).replace("\n", " ").replace("\t", " "))
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def _stamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    return _text(value, 80)


def _quote(label, value, limit=TEXT_LIMIT):
    # Every user/model supplied line stays in an indented code block; no active HTML or Markdown links.
    return f"{label}：\n\n" + "\n".join("    " + line for line in _text(value, limit).split("\n")) + "\n"


class _Document:
    def __init__(self):
        self.parts = []
        self.size = 0
        self.truncated = False

    def add(self, value):
        encoded = (value.rstrip() + "\n\n").encode("utf-8")
        if self.truncated or self.size + len(encoded) > MAX_REPORT_BYTES - 512:
            self.truncated = True
            return
        self.parts.append(encoded)
        self.size += len(encoded)

    def finish(self):
        if self.truncated:
            self.parts.append("\n报告达到 512 KiB 上限，后续记录已省略；以上内容不是完整档案。\n".encode())
        return b"".join(self.parts)


def _scope(plan_id, current_plan_id):
    return "当前计划" if current_plan_id and plan_id == current_plan_id else "历史计划（不代表当前进度）"


def _applicable(row, current_plan_id):
    return bool(
        current_plan_id and row.plan_id == current_plan_id and row.valid
        and row.submission_id == row.active_submission_id
        and row.submission_requirements == row.requirements_version == row.rubric_version
    )


async def render_learning_report(user_id: str, goal_id: str) -> LearningReport:
    from app.domain.verification import applicable_evidence

    try:
        canonical_id = str(UUID(goal_id))
    except (ValueError, AttributeError):
        raise DomainError("not_found", "资源不存在", 404) from None
    async with SessionFactory() as db:
        if db.bind.dialect.name == "postgresql":
            await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        goal = await db.scalar(select(Goal).where(Goal.id == canonical_id, Goal.user_id == user_id))
        if goal is None:
            raise DomainError("not_found", "资源不存在", 404)
        generated_at = now()
        current_plan = await db.scalar(select(Plan.id).where(
            Plan.id == goal.active_plan_id, Plan.goal_id == goal.id)) if goal.active_plan_id else None
        # A dangling/cross-goal active plan pointer cannot lend current status to any record.
        current_id = current_plan if current_plan else None
        plan = (await db.execute(select(Plan.version, Plan.status, func.substr(Plan.summary, 1, TEXT_LIMIT + 1))
                                .where(Plan.id == current_id, Plan.goal_id == goal.id))).first() if current_id else None
        current_group = case((Task.plan_id == current_id, True), else_=False)
        status_group = case((Task.status.in_(("ready", "in_progress", "awaiting_review", "completed",
            "needs_revision", "cancelled", "skipped")), Task.status), else_="other")
        counts = (await db.execute(select(current_group, status_group, func.count())
                                   .where(Task.goal_id == goal.id)
                                   .group_by(current_group, status_group))).all()
        selected_tasks = []
        task_columns = (Task.id, Task.plan_id, Task.title, Task.status, Task.scheduled_date,
                        Task.requirements_version, func.substr(Task.instructions, 1, 601).label("instructions"))
        for current, limit in ((True, CURRENT_TASK_LIMIT), (False, HISTORICAL_TASK_LIMIT)):
            predicate = Task.plan_id == current_id if current else Task.plan_id != current_id
            if current_id is None:
                if current:
                    continue
                predicate = Task.goal_id == goal.id
            selected_tasks.extend((await db.execute(select(*task_columns)
                .join(Plan, Task.plan_id == Plan.id)
                .where(Task.goal_id == goal.id, Plan.goal_id == goal.id, predicate)
                .order_by(Task.created_at.desc(), Task.id.desc()).limit(limit))).all())
        submission_scope = (
            Submission.user_id == user_id, Task.goal_id == goal.id, Plan.goal_id == goal.id,
        )
        submissions = (await db.execute(select(
            Submission.id, Submission.task_id, Submission.requirements_version,
            func.substr(Submission.text, 1, TEXT_LIMIT + 1).label("text"), Submission.created_at,
            Task.plan_id, Task.active_submission_id, Task.requirements_version.label("task_requirements"),
        ).join(Task, Submission.task_id == Task.id).join(Plan, Task.plan_id == Plan.id)
            .where(*submission_scope).order_by(Submission.created_at.desc(), Submission.id.desc())
            .limit(RECORD_LIMIT))).all()
        evidence_query = select(
            Evaluation.id, Evaluation.submission_id, Evaluation.passed, Evaluation.evidence_type,
            func.substr(Evaluation.feedback, 1, TEXT_LIMIT + 1).label("feedback"), Evaluation.valid,
            Evaluation.rubric_version, Evaluation.created_at, Task.plan_id, Task.active_submission_id,
            Task.requirements_version, Submission.requirements_version.label("submission_requirements"),
        ).join(Submission, Evaluation.submission_id == Submission.id).join(Task, Submission.task_id == Task.id)
        evidence_query = evidence_query.join(Plan, Task.plan_id == Plan.id).join(
            Operation, Evaluation.operation_id == Operation.id).where(
                *submission_scope, Operation.user_id == user_id, Operation.goal_id == goal.id,
                Operation.kind == "assessment")
        evaluations = (await db.execute(evidence_query.order_by(
            Evaluation.created_at.desc(), Evaluation.id.desc()).limit(RECORD_LIMIT))).all()
        progress = (await db.execute(select(
            SkillProgress.name, SkillProgress.status, SkillProgress.review_at, SkillProgress.evaluation_id,
            Evaluation.submission_id, Evaluation.valid, Evaluation.rubric_version, Task.plan_id,
            Task.active_submission_id, Task.requirements_version,
            Submission.requirements_version.label("submission_requirements"),
        ).join(Evaluation, SkillProgress.evaluation_id == Evaluation.id)
            .join(Submission, Evaluation.submission_id == Submission.id).join(Task, Submission.task_id == Task.id)
            .join(Plan, Task.plan_id == Plan.id).join(Operation, Evaluation.operation_id == Operation.id)
            .where(SkillProgress.user_id == user_id, SkillProgress.goal_id == goal.id,
                   Submission.user_id == user_id, Task.goal_id == goal.id, Plan.goal_id == goal.id,
                   Operation.user_id == user_id, Operation.goal_id == goal.id, Operation.kind == "assessment")
            .order_by(SkillProgress.review_at, SkillProgress.id).limit(RECORD_LIMIT))).all()
        receipt_scope = (Submission.user_id == user_id, Task.goal_id == goal.id)
        executions = (await db.execute(select(
            SandboxExecution.id, SandboxExecution.status, SandboxExecution.outcome,
            SandboxExecution.result_saved, SandboxExecution.cleanup_done, SandboxExecution.plan_id,
            SandboxExecution.submission_id, SandboxExecution.requirements_version,
            SandboxExecution.goal_revision, Task.active_submission_id,
            Task.requirements_version.label("task_requirements"), SandboxExecution.created_at,
        ).join(Submission, SandboxExecution.submission_id == Submission.id)
            .join(Task, SandboxExecution.task_id == Task.id)
            .where(SandboxExecution.user_id == user_id, SandboxExecution.goal_id == goal.id,
                   Submission.task_id == Task.id, *receipt_scope)
            .order_by(SandboxExecution.created_at.desc(), SandboxExecution.id.desc()).limit(20))).all()
        coding = (await db.execute(select(CodingRun.id, CodingRun.status, CodingRun.cleanup_done,
            CodingRun.plan_id, CodingRun.created_at, CodingRun.submission_id, CodingRun.goal_revision,
            CodingRun.requirements_version, Task.active_submission_id,
            Task.requirements_version.label("task_requirements"))
            .join(Submission, CodingRun.submission_id == Submission.id)
            .join(Task, CodingRun.task_id == Task.id)
            .where(CodingRun.user_id == user_id, CodingRun.goal_id == goal.id,
                   Submission.task_id == Task.id, *receipt_scope)
            .order_by(CodingRun.created_at.desc(), CodingRun.id.desc()).limit(20))).all()
        verified = await applicable_evidence(db, user_id, goal.id)

    doc = _Document()
    doc.add("# 学习记录报告")
    doc.add(f"生成时间（UTC）：{_stamp(generated_at)}\n\n目标 ID：{_inline(goal.id)}\n\n"
            f"目标状态：{_inline(goal.status)}；目标修订：{goal.control_revision}")
    doc.add("## 来源与解释边界\n\n本报告只读取当前登录用户此目标的数据库记录，不调用模型。"
            "任务 completed、技能 evidence_available、AI 给出的 passed 都是系统记录，不能据此认定已验证掌握。"
            "ai_review（AI 审阅）未执行用户代码；执行回执也不证明测试通过或技能掌握。"
            "历史计划、旧提交、旧要求或失效评估单独标记，不并入当前进度。"
            "未导出对话、语音字幕、记忆、模型内部历史、凭据、附件内容或执行 stdout/stderr。"
            "包含的目标、提交和反馈原文以代码块展示，供用户自行决定是否分享。")
    doc.add("## 目标快照\n\n" + _quote("标题", goal.title, 200) + "\n" + _quote("预期成果", goal.outcome))
    doc.add(_quote("完成标准", "\n".join(_text(item, 300) for item in (goal.criteria or [])[:20])))
    doc.add("## 当前计划\n\n" + (
        f"计划 ID：{_inline(current_id)}；版本：{plan.version}；状态：{_inline(plan.status)}\n\n"
        + _quote("计划摘要（已保存的 AI 提案）", plan[2]) if plan else "尚无有效的当前计划。"))
    doc.add("## 任务记录统计\n\n此处统计数据库任务状态，不统计掌握率。")
    summaries = {True: Counter(), False: Counter()}
    for row in counts:
        summaries[bool(row[0])][row[1]] += row[2]
    for current, summary in summaries.items():
        label = "当前计划" if current else "历史计划"
        doc.add(f"{label}：共 {sum(summary.values())} 项；" +
                ("、".join(f"{_inline(status)}={count}" for status, count in sorted(summary.items())) or "无任务"))
    doc.add("## 任务明细\n\n当前计划最多列 40 项，历史计划最多列 20 项，各组按创建时间倒序。")
    for task in selected_tasks:
        doc.add(f"### 任务 {_inline(task.id)}\n\n{_scope(task.plan_id, current_id)}；"
                f"状态：{_inline(task.status)}；日期：{task.scheduled_date}；要求版本：{task.requirements_version}\n\n"
                + _quote("标题", task.title, 200) + "\n" + _quote("要求摘录", task.instructions, 600))
    doc.add("## 最近提交\n\n最多列 40 条；仅含文本摘录，不读取附件。")
    for item in submissions:
        current = bool(current_id and item.plan_id == current_id and item.id == item.active_submission_id
                       and item.requirements_version == item.task_requirements)
        doc.add(f"### 提交 {_inline(item.id)}\n\n任务：{_inline(item.task_id)}；"
                f"{'当前提交 / 当前要求' if current else '历史提交（不代表当前进度）'}；"
                f"要求版本：{item.requirements_version}；时间：{_stamp(item.created_at)}\n\n" + _quote("文本摘录", item.text))
    doc.add("## 最近评估证据\n\n最多列 40 条。当前适用需同时匹配当前计划、当前提交、任务要求/评估量规版本且 valid=true。"
            "“当前适用”说明版本匹配，不表示人工验证或已掌握。")
    for item in evaluations:
        label = "当前适用（版本匹配）" if _applicable(item, current_id) else "历史证据（不代表当前进度）"
        doc.add(f"### 评估 {_inline(item.id)}\n\n{label}；提交：{_inline(item.submission_id)}；"
                f"passed：{str(item.passed).lower()}；valid：{str(item.valid).lower()}；"
                f"evidence_type：{_inline(item.evidence_type)}；量规版本：{item.rubric_version}；"
                f"时间：{_stamp(item.created_at)}\n\n" + _quote("反馈摘录（审阅意见）", item.feedback))
    doc.add("## 技能索引与复习日期\n\n最多列 40 条。技能状态是评估派生索引；请对照原始评估的当前/历史标签，不能当作已验证掌握。")
    for item in progress:
        label = "当前适用（版本匹配）" if _applicable(item, current_id) else "历史证据（不代表当前进度）"
        doc.add(_quote("技能", item.name, 200) + f"\n{label}；记录状态：{_inline(item.status)}；"
                f"来源评估：{_inline(item.evaluation_id)}；复习日期（UTC）：{_stamp(item.review_at)}")
    doc.add("## 执行与 Coder 回执\n\n各最多列 20 条。仅记录执行状态，不导出程序或输出，tests_verified=false；"
            "进程执行成功和 Coder 完成均不会自动验证任务标准或改变 ai_review 的性质。")
    for item in executions:
        current = bool(current_id and item.plan_id == current_id and item.submission_id == item.active_submission_id
                       and item.requirements_version == item.task_requirements and item.goal_revision == goal.control_revision)
        doc.add(f"执行 {_inline(item.id)}；{'当前提交回执' if current else '历史回执'}；"
                f"status={_inline(item.status)}；outcome={_inline(item.outcome)}；"
                f"result_saved={str(item.result_saved).lower()}；cleanup_done={str(item.cleanup_done).lower()}；"
                f"时间：{_stamp(item.created_at)}")
    for item in coding:
        current = bool(current_id and item.plan_id == current_id and item.submission_id == item.active_submission_id
                       and item.requirements_version == item.task_requirements and item.goal_revision == goal.control_revision)
        doc.add(f"Coder {_inline(item.id)}；{'当前提交回执' if current else '历史回执'}；"
                f"status={_inline(item.status)}；cleanup_done={str(item.cleanup_done).lower()}；"
                f"时间：{_stamp(item.created_at)}")
    doc.add("## 当前适用的样例验证\n\n最多列最近40条可信完整运行记录，绑定当前计划、当前提交、任务要求和最新确认规则。"
            "通过只表示确认的这些输入输出样例通过；失败也是实际测试证据。缺失退出码、截断、未知回执不列入此处。"
            "这些记录不会自动完成任务或证明全部知识已掌握，不导出代码、样例输入/预期答案和程序日志。")
    for item in verified:
        doc.add(f"验证 {_inline(item['id'])}；任务 {_inline(item['task_id'])}；"
                f"提交 {_inline(item['submission_id'])}；样例结果：{'通过' if item['passed'] else '未通过'}；"
                f"tests_verified=true；规则摘要 {_inline(item['contract_hash'])}；"
                f"代码SHA256 {_inline(item['source_sha256'])}；时间 {_stamp(item['created_at'])}")
    doc.add("## 报告范围\n\n各明细采用固定条数上限，文本摘录最多 1600 字符（任务要求 600），"
            "超长字段标记“内容已截断”，整份文件最多 512 KiB。未列出的记录不表示不存在。"
            "报告是导出时的只读快照；下载不会修改任务、评估、学习预算或目标状态。")
    return LearningReport(f"learning-report-{canonical_id}.md", doc.finish())
