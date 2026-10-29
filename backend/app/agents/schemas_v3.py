"""Separate output types leave the registered V1/V2 model schemas unchanged."""

from pydantic import Field

from app.agents.schemas import AssessmentProposal, PlanProposal, RecommendationProposal, TutorProposal
from app.agents.ui_v3 import DisplayProposalV3


class PlanProposalV3(PlanProposal):
    ui_components: list[DisplayProposalV3] = Field(default_factory=list, max_length=6)


class TutorProposalV3(TutorProposal):
    ui_components: list[DisplayProposalV3] = Field(default_factory=list, max_length=6)


class AssessmentProposalV3(AssessmentProposal):
    ui_components: list[DisplayProposalV3] = Field(default_factory=list, max_length=6)


class RecommendationProposalV3(RecommendationProposal):
    ui_components: list[DisplayProposalV3] = Field(default_factory=list, max_length=6)
