from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import router as auth_router
from app.api.routes import router
from app.db.session import SessionFactory, engine
from app.domain.common import DomainError


@asynccontextmanager
async def lifespan(app):
    yield
    await engine.dispose()


app = FastAPI(title="Learning Personal Agent", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(router, prefix="/api/v1")


@app.middleware("http")
async def request_metadata(request: Request, call_next):
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
    return response


def error(request, code, message, status):
    return JSONResponse(
        {"code": code, "message": message, "request_id": getattr(request.state, "request_id", str(uuid4()))},
        status_code=status,
    )


@app.exception_handler(DomainError)
async def domain_error(request, exc):
    return error(request, exc.code, exc.message, exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Do not echo password or request input values from Pydantic's error payload.
    return error(request, "validation_error", "请求字段无效，请检查必填内容和格式", 422)


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    return error(request, "database_unavailable", "数据服务暂不可用，请稍后重试", 503)


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request):
    try:
        async with SessionFactory() as db:
            version = await db.scalar(text("SELECT version_num FROM alembic_version"))
            if version != "0003_conversation_model":
                return error(request, "migration_required", "数据库迁移未完成", 503)
        return {"status": "ready", "database": "ready", "migration": version}
    except SQLAlchemyError:
        return error(request, "database_unavailable", "数据服务尚未就绪", 503)
