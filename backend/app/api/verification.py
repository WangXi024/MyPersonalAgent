from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.auth import current_user
from app.api.model_configs import request_key
from app.domain import verification as domain

router = APIRouter()


class SampleCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    stdin: str = Field(max_length=16384)
    expected_stdout: str = Field(max_length=16384)

    @field_validator("name", "stdin", "expected_stdout")
    @classmethod
    def text(cls, value):
        try:
            if "\x00" in value or len(value.encode("utf-8")) > 16384:
                raise ValueError("Use bounded UTF-8 text without NUL")
        except UnicodeError:
            raise ValueError("Use valid UTF-8 text") from None
        return value


class ContractInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_goal_revision: int = Field(ge=1)
    requirements_version: int = Field(ge=1)
    confirm: Literal[True]
    comparator: Literal["exact", "normalize_newlines"]
    cases: list[SampleCase] = Field(min_length=1, max_length=6)

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value


class VerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_goal_revision: int = Field(ge=1)
    contract_id: str = Field(min_length=1, max_length=36)
    expected_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attachment_id: str | None = Field(default=None, min_length=1, max_length=36)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/tasks/{task_id}/test-contracts", status_code=201)
async def confirm(task_id: str, payload: ContractInput, user=Depends(current_user), key=Depends(request_key)):
    return await domain.confirm_contract(user.id, task_id, payload.model_dump(), key)


@router.get("/tasks/{task_id}/test-contracts")
async def contracts(task_id: str, user=Depends(current_user)):
    return await domain.list_contracts(user.id, task_id)


@router.post("/submissions/{submission_id}/verification-runs", status_code=202)
async def create(submission_id: str, payload: VerificationInput, user=Depends(current_user), key=Depends(request_key)):
    return await domain.create_run(user.id, submission_id, payload.model_dump(), key)


@router.get("/goals/{goal_id}/verification")
async def for_goal(goal_id: str, user=Depends(current_user)):
    return await domain.goal_runs(user.id, goal_id)


@router.get("/verification-runs/{run_id}")
async def get(run_id: str, user=Depends(current_user)):
    return await domain.get_run(user.id, run_id)


@router.post("/verification-runs/{run_id}/cancel")
async def cancel(run_id: str, payload: EmptyInput, user=Depends(current_user), key=Depends(request_key)):
    return await domain.request_cancel(user.id, run_id, key)
