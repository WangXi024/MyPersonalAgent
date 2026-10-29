from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.api.auth import current_user
from app.domain.reports import render_learning_report


router = APIRouter()


@router.get("/goals/{goal_id}/report.md")
async def download_learning_report(goal_id: str, user=Depends(current_user)):
    report = await render_learning_report(user.id, goal_id)
    return Response(report.content, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{report.filename}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })
