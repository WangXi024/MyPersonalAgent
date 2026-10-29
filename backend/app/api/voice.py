"""Authenticated local voice configuration and session control; no provider calls on REST saves."""

import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.api.auth import COOKIE, current_user
from app.api.model_configs import admin, request_key
from app.domain import voice as domain

router = APIRouter()


class VoiceConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=0)
    provider: Literal["openai", "azure", "google", "xai"] | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    voice: str | None = Field(default=None, min_length=1, max_length=100)
    endpoint: str | None = Field(default=None, min_length=1, max_length=500)
    api_version: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool = False
    api_key: SecretStr | None = Field(default=None, min_length=8, max_length=4096)

    @field_validator("model_id", "voice", "api_version")
    @classmethod
    def identifiers(cls, value):
        if value is not None and any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Identifier must not contain whitespace or control characters")
        if value is not None:
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("Identifier must be valid UTF-8") from None
        return value

    @field_validator("model_id")
    @classmethod
    def model_identifier(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value):
            raise ValueError("Use an explicit native realtime model identifier")
        return value

    @field_validator("endpoint")
    @classmethod
    def azure_endpoint(cls, value):
        if value is not None:
            parts = urlsplit(value)
            if (parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.openai\.azure\.com", parts.hostname or "")
                    or parts.path not in {"", "/"} or parts.query or parts.fragment):
                raise ValueError("Use the official Azure OpenAI HTTPS endpoint")
            return value.rstrip("/")
        return value

    @field_validator("api_key")
    @classmethod
    def secret(cls, value):
        if value is not None:
            raw = value.get_secret_value()
            if (not raw.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "<>" for c in raw)
                    or raw.startswith("os.environ/")):
                raise ValueError("Use an actual provider key without placeholders")
        return value


class VoiceSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_config_version: int = Field(ge=1)
    expected_goal_revision: int | None = Field(default=None, ge=1)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def login_session(request: Request, user=Depends(current_user)):
    auth = await domain.authenticate_login(request.cookies.get(COOKIE))
    return auth["auth_session_id"]


@router.get("/voice/config")
async def config(user=Depends(admin)):
    return await domain.get_config()


@router.put("/voice/config")
async def save_config(payload: VoiceConfigInput, user=Depends(admin), key=Depends(request_key)):
    return await domain.save_config(user.id, payload.model_dump(), key)


@router.get("/voice/capabilities")
async def capabilities(conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
                       user=Depends(current_user)):
    return await domain.capabilities(user.id, conversation_id)


@router.post("/conversations/{conversation_id}/voice-sessions", status_code=201)
async def create_session(conversation_id: str, payload: VoiceSessionInput, user=Depends(current_user),
                         auth_session_id=Depends(login_session), key=Depends(request_key)):
    return await domain.create_session(user.id, conversation_id, payload.model_dump(), key, auth_session_id)


@router.post("/voice-sessions/{session_id}/connect-ticket")
async def connect_ticket(session_id: str, payload: EmptyInput, user=Depends(current_user),
                         auth_session_id=Depends(login_session)):
    return await domain.mint_ticket(user.id, session_id, auth_session_id)


@router.get("/voice-sessions/{session_id}")
async def session(session_id: str, user=Depends(current_user)):
    return await domain.get_session(user.id, session_id)


@router.post("/voice-sessions/{session_id}/stop")
async def stop_session(session_id: str, payload: EmptyInput, user=Depends(current_user), key=Depends(request_key)):
    return await domain.request_stop(user.id, session_id, key)
