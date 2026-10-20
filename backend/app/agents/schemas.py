from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=2000)
    message: str = Field(min_length=1, max_length=12000)


class MilestoneProposal(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    criteria: list[str] = Field(min_length=1, max_length=10)


class TaskProposal(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=8000)
    estimated_minutes: int = Field(ge=5, le=120)
    scheduled_date: date
    milestone_index: int = Field(ge=0)
    criteria: list[str] = Field(min_length=1, max_length=10)
    skills: list[str] = Field(default_factory=list, max_length=10)
    review_of_task_id: str | None = None


class PlanProposal(Proposal):
    kind: Literal["plan"] = "plan"
    milestones: list[MilestoneProposal] = Field(min_length=1, max_length=8)
    tasks: list[TaskProposal] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid_milestones(self):
        if any(t.milestone_index >= len(self.milestones) for t in self.tasks):
            raise ValueError("Every task must refer to an existing milestone")
        return self


class TutorFollowUp(BaseModel):
    kind: Literal["plan", "recommendation"]
    instruction: str = Field(min_length=1, max_length=2000)
    today_minutes: int | None = Field(default=None, ge=1, le=1440)


class TutorProposal(Proposal):
    kind: Literal["tutor"] = "tutor"
    follow_up: TutorFollowUp | None = None


class SkillEvidence(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    passed: bool


class AssessmentProposal(Proposal):
    kind: Literal["assessment"] = "assessment"
    submission_id: str
    passed: bool
    feedback: str = Field(min_length=1, max_length=12000)
    evidence_type: Literal["ai_review"] = "ai_review"
    skills: list[SkillEvidence] = Field(default_factory=list, max_length=20)


class RecommendationProposal(Proposal):
    kind: Literal["recommendation"] = "recommendation"
    task: TaskProposal | None = None
    task_id: str | None = None
