"""V3 presentation proposals. Answer keys stay private; progress is hydrated from SQL."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import func, select

from app.agents.ui import ChecklistDisplay, ResourceDisplay, build_ui_components
from app.db.models import Evaluation, Goal, SkillProgress, Submission, Task, now
from app.domain.common import DomainError


class QuizChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: Literal["a", "b", "c", "d"]
    text: str = Field(min_length=1, max_length=500)


class QuizQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=1000)
    choices: list[QuizChoice] = Field(min_length=2, max_length=4)
    correct_choice_id: Literal["a", "b", "c", "d"]
    explanation: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def answer_exists(self):
        ids = [item.id for item in self.choices]
        if len(set(ids)) != len(ids) or self.correct_choice_id not in ids:
            raise ValueError("Choices must be unique and include the answer")
        return self


class QuizDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["quiz"]
    title: str = Field(min_length=1, max_length=160)
    questions: list[QuizQuestion] = Field(min_length=1, max_length=5)


class ProgressDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["progress"]
    title: str = Field(min_length=1, max_length=160)


DisplayProposalV3 = Annotated[ChecklistDisplay | ResourceDisplay | QuizDisplay | ProgressDisplay,
                               Field(discriminator="type")]
DISPLAY_V3 = TypeAdapter(list[DisplayProposalV3])


async def progress_snapshot(db, op):
    from app.domain.verification import applicable_evidence

    goal = await db.get(Goal, op.goal_id) if op.goal_id else None
    if not goal or goal.user_id != op.user_id:
        raise DomainError("invalid_progress_scope", "进度组件仅用于当前目标对话", 422)
    counts = dict((await db.execute(select(Task.status, func.count()).where(
        Task.goal_id == goal.id, Task.plan_id == goal.active_plan_id).group_by(Task.status))).all()) if goal.active_plan_id else {}
    statuses = ("completed", "in_progress", "awaiting_review", "needs_revision", "ready", "skipped")
    tasks = {status: counts.get(status, 0) for status in statuses}
    tasks["total"] = sum(count for status, count in counts.items() if status != "cancelled")
    applicable = (select(Evaluation.id).select_from(Evaluation)
        .join(Submission, Evaluation.submission_id == Submission.id).join(Task, Submission.task_id == Task.id)
        .where(Task.goal_id == goal.id, Task.plan_id == goal.active_plan_id,
            Task.active_submission_id == Submission.id, Submission.user_id == op.user_id,
            Evaluation.valid.is_(True), Evaluation.rubric_version == Task.requirements_version,
            Submission.requirements_version == Task.requirements_version, Evaluation.evidence_type == "ai_review"))
    evaluation_ids = list(await db.scalars(applicable)) if goal.active_plan_id else []
    skills = list(await db.scalars(select(SkillProgress).where(SkillProgress.user_id == op.user_id,
        SkillProgress.goal_id == goal.id, SkillProgress.evaluation_id.in_(evaluation_ids)))) if evaluation_ids else []
    verified = await applicable_evidence(db, op.user_id, goal.id)
    return {"goal_id": goal.id, "title": goal.title, "goal_status": goal.status,
        "goal_revision": goal.control_revision, "plan_id": goal.active_plan_id, "as_of": now().isoformat(),
        "tasks": tasks, "skills": {"total": len(skills), "needs_review": sum(item.status == "needs_review" for item in skills)},
        "evidence": {"ai_review": len(evaluation_ids), "verified_tests": len(verified)}}


async def build_v3_ui(db, op, proposals, sources):
    try:
        specs = DISPLAY_V3.validate_python(proposals)
    except ValidationError:
        raise DomainError("invalid_ui", "展示组件的数据格式无效", 422) from None
    if len(specs) > 6:
        raise DomainError("invalid_ui", "展示组件数量超出限制", 422)
    components, private = [], {}
    for index, spec in enumerate(specs, 1):
        identifier = f"display-{index}"
        if isinstance(spec, (ChecklistDisplay, ResourceDisplay)):
            block = build_ui_components([spec.model_dump()], sources)[0]
            block["id"] = identifier
        elif isinstance(spec, ProgressDisplay):
            block = {"type": "progress", "id": identifier, "title": spec.title,
                     "snapshot": await progress_snapshot(db, op)}
        else:
            if op.kind != "tutor":
                raise DomainError("invalid_quiz_scope", "互动练习仅用于辅导对话", 422)
            quiz_id = f"quiz-{index}"
            questions = [{"id": f"q{position}", **question.model_dump()} for position, question in enumerate(spec.questions, 1)]
            private[quiz_id] = {"questions": questions, "attempts": []}
            block = {"type": "quiz", "id": identifier, "operation_id": op.id, "quiz_id": quiz_id,
                "title": spec.title, "practice_only": True, "attempt_count": 0, "max_attempts": 5,
                "attempt": None, "questions": [{key: question[key] for key in ("id", "prompt", "choices")} for question in questions]}
        components.append(block)
    return components, private
