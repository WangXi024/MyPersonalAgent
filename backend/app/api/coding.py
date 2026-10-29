from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.auth import current_user
from app.api.routes import key
from app.db.models import CodingArtifact, CodingRun, Submission
from app.db.session import SessionFactory
from app.domain import coding_runs as domain
from app.domain.common import DomainError, owned
from app.domain.sandbox_execution import storage_path

router = APIRouter()


class CodingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_goal_revision: int = Field(ge=1)
    attachment_id: str | None = Field(default=None, min_length=1, max_length=36)
    instruction: str = Field(min_length=1, max_length=4000)


@router.post("/submissions/{submission_id}/coding-runs", status_code=202)
async def create(submission_id: str, payload: CodingInput, user=Depends(current_user), request_key=Depends(key)):
    return await domain.create_run(user.id, submission_id, payload.model_dump(), request_key)


@router.get("/submissions/{submission_id}/coding-runs")
async def for_submission(submission_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        await owned(db, Submission, submission_id, user.id)
        rows = await db.scalars(select(CodingRun).where(CodingRun.submission_id == submission_id,
            CodingRun.user_id == user.id).order_by(CodingRun.created_at.desc()).limit(30))
        return {"items": [await domain.public_run(db, item) for item in rows]}


@router.get("/coding-runs/{run_id}")
async def get_run(run_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        return await domain.public_run(db, await owned(db, CodingRun, run_id, user.id))


@router.post("/coding-runs/{run_id}/cancel")
async def cancel(run_id: str, user=Depends(current_user), request_key=Depends(key)):
    return await domain.request_cancel(user.id, run_id, request_key)


@router.get("/coding-runs/{run_id}/artifacts/{artifact_id}")
async def artifact(run_id: str, artifact_id: str, user=Depends(current_user)):
    async with SessionFactory() as db:
        await owned(db, CodingRun, run_id, user.id)
        item = await owned(db, CodingArtifact, artifact_id, user.id)
        if item.run_id != run_id:
            raise DomainError("not_found", "生成文件不存在", 404)
        path = storage_path(item.storage_key)
        if not path.is_file():
            raise DomainError("file_missing", "生成文件暂不可用", 503)
        return FileResponse(path, filename=item.filename.rsplit("/", 1)[-1], media_type="application/octet-stream",
                            headers={"Content-Security-Policy": "sandbox"})
