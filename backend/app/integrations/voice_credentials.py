"""Local authenticated encryption for voice keys; this module never performs network IO."""

import base64
import hashlib
import json
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from app.domain.common import DomainError
from app.integrations.settings import runtime_settings


def _master():
    try:
        path = runtime_settings().voice_credential_key_path
        with path.open("rt", encoding="ascii") as handle:
            raw = handle.read(129)
        if len(raw) > 128:
            raise ValueError("Invalid key file size")
        encoded = raw.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}=?", encoded):
            raise ValueError("Invalid key encoding")
        key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(key) != 32:
            raise ValueError("Invalid key size")
        return key
    except (OSError, UnicodeError, ValueError):
        raise DomainError("voice_credentials_unavailable", "本机语音密钥存储尚未就绪，请检查部署配置", 503) from None


def _aad(config_id, provider, version):
    return json.dumps({"schema": "voice-aesgcm-v1", "config_id": config_id, "provider": provider,
                       "version": version}, sort_keys=True, separators=(",", ":")).encode()


def seal(secret: SecretStr, config_id: str, provider: str, version: int):
    key, nonce = _master(), os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, secret.get_secret_value().encode(), _aad(config_id, provider, version))
    return ciphertext, nonce, hashlib.sha256(key).hexdigest()


def open_secret(ciphertext, nonce, key_id, config_id, provider, version) -> SecretStr:
    key = _master()
    try:
        if key_id != hashlib.sha256(key).hexdigest() or len(nonce or b"") != 12:
            raise ValueError("Key does not match")
        return SecretStr(AESGCM(key).decrypt(nonce, ciphertext, _aad(config_id, provider, version)).decode())
    except (InvalidTag, UnicodeError, ValueError, TypeError):
        raise DomainError("voice_credentials_unavailable", "语音密钥无法解密，请检查本机密钥或重新保存配置", 503) from None


def ready(key_id: str | None = None) -> bool:
    try:
        key = _master()
        return key_id is None or key_id == hashlib.sha256(key).hexdigest()
    except DomainError:
        return False
