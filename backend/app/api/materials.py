from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response

from app.api.auth import current_user
from app.api.routes import key
from app.domain import materials as domain
from app.integrations.materials import MAX_INPUT


router = APIRouter(prefix="/materials")


@router.get("/capabilities")
async def capabilities(user=Depends(current_user)):
    return domain.capabilities()


@router.post("", status_code=202)
async def upload(file: UploadFile = File(...), user=Depends(current_user), request_key=Depends(key)):
    return await domain.create_material(user.id, file.filename, await file.read(MAX_INPUT + 1), request_key)


@router.get("")
async def materials(user=Depends(current_user)):
    return await domain.list_materials(user.id)


@router.get("/{operation_id}")
async def material(operation_id: str, user=Depends(current_user)):
    return await domain.get_material(user.id, operation_id)


@router.post("/{operation_id}/cancel")
async def cancel(operation_id: str, user=Depends(current_user), request_key=Depends(key)):
    return await domain.request_cancel(user.id, operation_id, request_key)


@router.get("/{operation_id}/preview")
async def preview(operation_id: str, user=Depends(current_user)):
    return await domain.preview(user.id, operation_id)


@router.get("/{operation_id}/download/{kind}")
async def download(operation_id: str, kind: str, user=Depends(current_user)):
    content, filename, mime = await domain.download(user.id, operation_id, kind)
    return Response(content, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
