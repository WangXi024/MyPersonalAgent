"""Native realtime providers. Local readiness never constructs clients or opens sockets."""
from contextlib import asynccontextmanager
from importlib.util import find_spec
import re
from urllib.parse import urlsplit

from pydantic import SecretStr


PROVIDERS = {
    "openai": ("OpenAI Realtime", ("openai", "websockets")),
    "azure": ("Azure OpenAI Realtime", ("openai", "websockets")),
    "google": ("Google Gemini Live", ("google.genai", "websockets")),
    "xai": ("xAI Grok Voice", ("xai_sdk", "openai", "websockets")),
}


class RealtimeUnavailable(Exception):
    """Safe public category; never include an underlying SDK exception or credentials."""

    def __init__(self, code="provider_unavailable"):
        self.code = code
        super().__init__(code)


def _installed(name):
    try:
        return find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def provider_readiness(provider):
    if provider is None:
        return {"supported": False, "dependency_ready": False, "reason": "provider_not_configured"}
    if provider not in PROVIDERS:
        return {"supported": False, "dependency_ready": False, "reason": "provider_unsupported"}
    ready = all(_installed(name) for name in PROVIDERS[provider][1])
    return {"supported": True, "dependency_ready": ready,
            "reason": "ready" if ready else "dependency_unavailable"}


def public_provider_options():
    return [{"id": key, "name": value[0], "dependency_ready": provider_readiness(key)["dependency_ready"]}
            for key, value in PROVIDERS.items()]


def _credentials(values):
    provider = values.get("provider")
    if not provider_readiness(provider)["dependency_ready"]:
        raise RealtimeUnavailable("dependency_unavailable")
    model = values.get("model_id")
    key = values.get("api_key")
    key = key.get_secret_value() if isinstance(key, SecretStr) else key
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", model)
            or not isinstance(key, str) or not key.strip()):
        raise RealtimeUnavailable("invalid_voice_configuration")
    return provider, model, key


def _azure_endpoint(value):
    if not isinstance(value, str):
        raise RealtimeUnavailable("invalid_voice_configuration")
    parsed = urlsplit(value)
    try:
        valid = (parsed.scheme == "https" and parsed.hostname is not None
                 and parsed.hostname.endswith(".openai.azure.com") and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise RealtimeUnavailable("invalid_voice_configuration")
    return value.rstrip("/")


@asynccontextmanager
async def native_model(values):
    """Only an authenticated, atomically reserved session may call this factory.

    Keys, native model and endpoint are explicit. No infer_model, chat configuration,
    environment credential fallback or OpenAI-compatible arbitrary URL is accepted.
    Client construction itself is local; the Agent session owns the live connection.
    """
    provider, model_id, key = _credentials(values)
    clients = []
    try:
        settings = {"output_modality": "audio", "tool_choice": "none", "max_tokens": 2048,
                    "turn_detection": True, "input_transcription_model": "auto"}
        voice = values.get("voice")
        if provider in {"openai", "azure"}:
            import httpx
            from openai import AsyncOpenAI
            from pydantic_ai.providers.openai import OpenAIProvider
            from pydantic_ai.realtime.openai import OpenAIRealtimeModel

            base = "https://api.openai.com/v1" if provider == "openai" else _azure_endpoint(values.get("endpoint")) + "/openai/v1"
            if provider == "openai" and values.get("base_url") not in (None, "https://api.openai.com/v1", "https://api.openai.com/v1/"):
                raise RealtimeUnavailable("invalid_voice_configuration")
            http = httpx.AsyncClient(timeout=10, trust_env=False)
            clients.append(http)
            client = AsyncOpenAI(api_key=key, base_url=base, organization="", project="", max_retries=0,
                                 http_client=http, timeout=10)
            clients.append(client)
            if voice:
                settings["openai_voice"] = voice
            if provider == "openai":
                model = OpenAIRealtimeModel(model_id, provider=OpenAIProvider(openai_client=client), settings=settings)
            else:
                from pydantic_ai.providers.azure import AzureProvider
                from pydantic_ai.realtime.azure import AzureRealtimeModel

                # Azure's native GA route itself uses AsyncOpenAI, rather than the
                # api-version query added by AsyncAzureOpenAI. Supply all auxiliary
                # credentials explicitly and force GA; Voice Live is a separate API.
                endpoint = _azure_endpoint(values.get("endpoint"))
                version = values.get("api_version")
                if not isinstance(version, str) or not version:
                    raise RealtimeUnavailable("invalid_voice_configuration")
                azure = AzureProvider(openai_client=client, voice_live_endpoint=endpoint,
                                      voice_live_api_key=key, voice_live_api_version=version)
                model = AzureRealtimeModel(model_id, provider=azure,
                    settings=settings | {"azure_voice_live": False}, profile={"azure_realtime_apis": ["azure_openai"]})
        elif provider == "google":
            from pydantic_ai.providers.google import GoogleProvider
            from pydantic_ai.realtime.google import GoogleRealtimeModel

            if voice:
                settings["google_voice"] = voice
            google = GoogleProvider(api_key=key, base_url="https://generativelanguage.googleapis.com")
            clients.append(google.client.aio)
            model = GoogleRealtimeModel(model_id, provider=google, settings=settings)
        else:
            from pydantic_ai.providers.xai import XaiProvider
            from pydantic_ai.realtime.xai import XaiRealtimeModel

            if voice:
                settings["xai_voice"] = voice
            model = XaiRealtimeModel(model_id, provider=XaiProvider(api_key=key, timeout=10), settings=settings)
        yield model
    finally:
        # SDK/client repr and error messages can contain live URLs or key material.
        # Cleanup errors are represented only by the caller's fixed safe category.
        failed = False
        for client in reversed(clients):
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close:
                try:
                    await close()
                except Exception:
                    failed = True
        if failed:
            raise RealtimeUnavailable("provider_cleanup_failed")
