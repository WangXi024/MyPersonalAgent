import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import LoginAttempt, Session, User, now
from app.db.session import SessionFactory
from app.domain.common import DomainError, public_user

router = APIRouter(prefix="/auth")
password_hasher = PasswordHasher()
DUMMY_HASH = password_hasher.hash(secrets.token_urlsafe(24))
COOKIE = "learning_session"
CSRF_COOKIE = "learning_csrf"


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def origin_check(request):
    if request.headers.get("origin", "").rstrip("/") != get_settings().public_url.rstrip("/"):
        raise DomainError("origin_rejected", "请求来源不受信任", 403)


async def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        raise DomainError("unauthenticated", "请先登录", 401)
    async with SessionFactory() as db:
        session = await db.scalar(
            select(Session).where(
                Session.token_hash == token_hash(token),
                Session.revoked.is_(False),
                Session.expires_at > now(),
            )
        )
        user = await db.get(User, session.user_id) if session else None
        if user is None or not user.active:
            raise DomainError("unauthenticated", "登录已过期，请重新登录", 401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin_check(request)
            csrf = request.headers.get("x-csrf-token", "")
            if not csrf or not secrets.compare_digest(token_hash(csrf), session.csrf_hash):
                raise DomainError("csrf_rejected", "安全令牌已过期，请刷新页面", 403)
        return user


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/login")
async def login(payload: Credentials, request: Request, response: Response):
    origin_check(request)
    address = request.client.host if request.client else "unknown"
    rate_key = token_hash(f"{address}:{payload.username.casefold()}")
    async with SessionFactory() as db, db.begin():
        count = await db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(LoginAttempt.key_hash == rate_key, LoginAttempt.created_at > now() - timedelta(minutes=15))
        )
        if count >= 10:
            raise DomainError("rate_limited", "登录尝试过于频繁，请稍后重试", 429)
        db.add(LoginAttempt(key_hash=rate_key))
    async with SessionFactory() as db, db.begin():
        user = await db.scalar(select(User).where(User.username == payload.username))
        try:
            password_hasher.verify(user.password_hash if user else DUMMY_HASH, payload.password)
            valid = user is not None and user.active
        except VerificationError:
            valid = False
        if not valid:
            raise DomainError("invalid_credentials", "用户名或密码错误", 401)
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        db.add(
            Session(
                user_id=user.id,
                token_hash=token_hash(token),
                csrf_hash=token_hash(csrf),
                expires_at=now() + timedelta(days=get_settings().session_days),
            )
        )
        options = dict(
            secure=get_settings().secure_cookies,
            samesite="strict",
            path="/",
            max_age=get_settings().session_days * 86400,
        )
        response.set_cookie(COOKIE, token, httponly=True, **options)
        response.set_cookie(CSRF_COOKIE, csrf, httponly=True, **options)
        return {"user": public_user(user), "csrf_token": csrf}


@router.get("/me")
async def me(request: Request, user=Depends(current_user)):
    return {"user": public_user(user), "csrf_token": request.cookies.get(CSRF_COOKIE)}


@router.post("/logout")
async def logout(request: Request, response: Response, user=Depends(current_user)):
    async with SessionFactory() as db, db.begin():
        session = await db.scalar(
            select(Session).where(Session.token_hash == token_hash(request.cookies[COOKIE]))
        )
        session.revoked = True
    response.delete_cookie(COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"status": "logged_out"}
