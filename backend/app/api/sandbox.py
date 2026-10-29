from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.auth import current_user
from app.api.routes import key
from app.db.models import SandboxArtifact, SandboxExecution, Submission
from app.db.session import SessionFactory
from app.domain.common import DomainError, owned
from app.domain import sandbox_execution as domain

router = APIRouter()


class ExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_goal_revision: int = Field(ge=1)
    language: Literal["python"] = "python"
    attachment_id: str | None = Field(default=None, min_length=1, max_length=36)


@router.post("/submissions/{submission_id}/executions", status_code=202)
async def create_execution(submission_id: str, payload: ExecutionInput, user=Depends(current_user),
                           request_key=Depends(key)):
    return await domain.create_execution(user.id, submission_id, payload.model_dump(), request_key)


@router.get("/submissions/{submission_id}/executions")
async def executions(submission_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        await owned(db, Submission, submission_id, user.id)
        rows = await db.scalars(select(SandboxExecution).where(SandboxExecution.submission_id == submission_id,
                               SandboxExecution.user_id == user.id).order_by(SandboxExecution.created_at.desc()).limit(30))
        return {"items": [await domain.public_execution(db, item) for item in rows]}


@router.get("/sandbox-executions/{execution_id}")
async def execution(execution_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        return await domain.public_execution(db, await owned(db, SandboxExecution, execution_id, user.id))


@router.post("/sandbox-executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str, user=Depends(current_user), request_key=Depends(key)):
    return await domain.request_cancel(user.id, execution_id, request_key)


@router.get("/sandbox-executions/{execution_id}/artifacts/{artifact_id}")
async def artifact(execution_id: str, artifact_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        await owned(db, SandboxExecution, execution_id, user.id)
        item = await owned(db, SandboxArtifact, artifact_id, user.id)
        if item.execution_id != execution_id:
            raise DomainError("not_found", "产物不存在", 404)
        path = domain.storage_path(item.storage_key)
        if not path.is_file():
            raise DomainError("file_missing", "产物暂不可用", 503)
        return FileResponse(path, filename=item.filename.rsplit("/", 1)[-1],
                            media_type="application/octet-stream", headers={"Content-Security-Policy": "sandbox"})
