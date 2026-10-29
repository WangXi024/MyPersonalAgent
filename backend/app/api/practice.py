from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import current_user
from app.api.model_configs import request_key
from app.domain.practice import submit_attempt

router = APIRouter()


class QuizAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(pattern=r"^q[1-5]$")
    choice_id: Literal["a", "b", "c", "d"]


class QuizAttemptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[QuizAnswer] = Field(min_length=1, max_length=5)


@router.post("/operations/{operation_id}/quizzes/{quiz_id}/attempts")
async def attempt(operation_id: str, quiz_id: str, payload: QuizAttemptInput,
                  user=Depends(current_user), key=Depends(request_key)):
    return await submit_attempt(user.id, operation_id, quiz_id, payload.model_dump()["answers"], key)
