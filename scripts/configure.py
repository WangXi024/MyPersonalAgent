"""Validate local literal dotenv configuration and generate protected runtime files."""
import argparse
import json
import math
import os
import re
import secrets
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlsplit


def read_env(path):
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"Invalid dotenv syntax at line {number}")
        value = value.strip()
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unclosed quote at line {number}")
            value = value[1:-1]
        # Literal values only. Docker interpolation must not reinterpret credentials.
        if "$" in value or "\x00" in value or "\\" in value:
            raise ValueError(f"Use literal values without dollar/backslash at line {number}")
        values[key] = value
    return values


def valid(value):
    return bool(value) and not any(x in value.lower() for x in ("<", ">", "changeme", "your-api", "example-password"))


def configure(env_file, check_only=False, infrastructure_only=False):
    cfg = read_env(env_file)
    required = ["APP_PUBLIC_URL", "BOOTSTRAP_USERNAME", "BOOTSTRAP_PASSWORD", "MODEL_PROVIDER"]
    primary = cfg.get("MODEL_PROVIDER", "")
    fallback = cfg.get("MODEL_FALLBACK_PROVIDER", "")
    if primary not in ("bailian", "zhipu", "ark"):
        raise ValueError("MODEL_PROVIDER must be bailian, zhipu, or ark")
    if fallback and (fallback not in ("bailian", "zhipu", "ark") or fallback == primary):
        raise ValueError("MODEL_FALLBACK_PROVIDER must identify a different supported provider")
    if fallback and cfg.get("MODEL_FALLBACK_VERIFIED") != "true":
        raise ValueError("Fallback requires MODEL_FALLBACK_VERIFIED=true after capability tests")
    for provider in filter(None, (primary, fallback)):
        prefix = provider.upper()
        required.extend([prefix + "_API_BASE", prefix + "_MODEL"])
        if not infrastructure_only:
            required.append(prefix + "_API_KEY")
            required.extend([prefix + "_INPUT_USD_PER_MILLION", prefix + "_OUTPUT_USD_PER_MILLION"])
        endpoint = urlsplit(cfg.get(prefix + "_API_BASE", ""))
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment:
            raise ValueError(prefix + "_API_BASE must be an HTTPS API base URL")
        if "coding" in endpoint.path.lower() or "coding" in endpoint.hostname:
            raise ValueError(prefix + "_API_BASE must use standard inference, not Coding Plan")
    missing = [name for name in required if not valid(cfg.get(name, ""))]
    if missing:
        raise ValueError("Missing or placeholder values: " + ", ".join(missing))
    if len(cfg["BOOTSTRAP_PASSWORD"]) < 16:
        raise ValueError("BOOTSTRAP_PASSWORD must contain at least 16 characters")
    for provider in filter(None, (primary, fallback)):
        for direction in ("INPUT", "OUTPUT"):
            name = provider.upper() + "_" + direction + "_USD_PER_MILLION"
            if valid(cfg.get(name, "")):
                try:
                    if not 0 < float(cfg[name]) < 1000000:
                        raise ValueError()
                except ValueError:
                    raise ValueError(name + " must be a positive finite USD price") from None
    origin = urlsplit(cfg["APP_PUBLIC_URL"])
    tls = cfg.get("TLS_MODE", "local")
    bind = cfg.get("APP_BIND_HOST", "127.0.0.1")
    if origin.username or origin.path not in ("", "/") or origin.query or origin.fragment:
        raise ValueError("APP_PUBLIC_URL must be an origin without credentials/path/query")
    if tls not in ("local", "upstream", "nginx"):
        raise ValueError("TLS_MODE must be local, upstream, or nginx")
    if tls == "local":
        if origin.hostname not in ("localhost", "127.0.0.1", "::1") or bind not in ("127.0.0.1", "::1") or origin.scheme != "http":
            raise ValueError("HTTP local mode must bind loopback and use a loopback public URL")
    elif origin.scheme != "https":
        raise ValueError("Public deployment requires APP_PUBLIC_URL=https://... and TLS")
    if tls == "upstream" and bind not in ("127.0.0.1", "::1"):
        raise ValueError("Upstream TLS mode must keep the plaintext backend on loopback")
    if tls == "nginx" and not all(Path(".secrets/tls", f).is_file() for f in ("fullchain.pem", "privkey.pem")):
        raise ValueError("TLS_MODE=nginx requires .secrets/tls/fullchain.pem and privkey.pem")
    for name in ("APP_MODEL_MONTHLY_BUDGET", "MEMORY_MODEL_MONTHLY_BUDGET"):
        try:
            if not math.isfinite(float(cfg.get(name, "0"))) or float(cfg.get(name, "0")) <= 0:
                raise ValueError()
        except ValueError:
            raise ValueError(name + " must be positive") from None
    for name, default in (("APP_PORT", "8080"), ("APP_TLS_PORT", "8443"), ("MODEL_REQUEST_TIMEOUT", "630"), ("DEPLOY_TIMEOUT", "1800")):
        if not cfg.get(name, default).isdigit() or int(cfg.get(name, default)) < 1:
            raise ValueError(name + " must be a positive integer")
    if check_only:
        print("Configuration validation passed; no secrets or model calls were made.")
        return
    os.umask(0o077)
    for name in (".secrets", ".runtime"):
        Path(name).mkdir(exist_ok=True)
        Path(name).chmod(0o700)
    for name in ("core_postgres_password", "memory_postgres_password", "app_db_password", "temporal_db_password", "visibility_db_password", "litellm_db_password", "memory_db_password", "litellm_master_key", "litellm_salt_key"):
        path = Path(".secrets", name)
        if not path.exists():
            path.write_text(("sk-" if "key" in name else "") + secrets.token_hex(32))
        # Parent directory restricts host access; service bind mounts remain readable.
        path.chmod(0o444)
    external = {"bootstrap_password": cfg["BOOTSTRAP_PASSWORD"]}
    external.update({p + "_api_key": cfg.get(p.upper() + "_API_KEY", "") if valid(cfg.get(p.upper() + "_API_KEY", "")) else "" for p in ("bailian", "zhipu", "ark")})
    for name, value in external.items():
        path = Path(".secrets", name)
        if path.exists():
            path.chmod(0o600)
        path.write_text(value)
        path.chmod(0o444)
    models = []
    chat_models = []
    # Leave headroom for 600-second chat/planning requests, including older .env files.
    gateway_timeout = max(630, int(cfg.get("MODEL_REQUEST_TIMEOUT", "630")))
    for alias, provider in (("learning-agent", primary), ("learning-fallback", fallback)):
        if provider:
            prefix = provider.upper()
            model = {"model_name": alias, "litellm_params": {"model": "openai/" + cfg[prefix + "_MODEL"], "api_base": cfg[prefix + "_API_BASE"], "api_key": "os.environ/" + prefix + "_API_KEY", "timeout": gateway_timeout}}
            costs = {direction.lower() + "_cost_per_token": float(cfg[prefix + "_" + direction + "_USD_PER_MILLION"]) / 1000000 for direction in ("INPUT", "OUTPUT") if valid(cfg.get(prefix + "_" + direction + "_USD_PER_MILLION", ""))}
            if costs:
                model["model_info"] = {"mode": "chat", **costs}
            models.append(model)
            display = {"id": alias, "name": cfg[prefix + "_MODEL"], "provider": provider,
                       "api_base": cfg[prefix + "_API_BASE"], "usd_cny_rate": "7"}
            for direction in ("INPUT", "OUTPUT"):
                price = cfg.get(prefix + "_" + direction + "_USD_PER_MILLION", "")
                if valid(price):
                    display[direction.lower() + "_cny_per_million"] = str((Decimal(price) * 7).quantize(Decimal("0.000001")))
            chat_models.append(display)
    config = {"model_list": models, "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY", "database_url": "os.environ/DATABASE_URL", "store_model_in_db": True}, "litellm_settings": {"drop_params": False, "num_retries": 0, "set_verbose": False, "json_logs": True, "turn_off_message_logging": True}, "router_settings": {"num_retries": 0, "timeout": gateway_timeout}}
    if fallback:
        config["router_settings"]["fallbacks"] = [{"learning-agent": ["learning-fallback"]}]
    Path(".runtime/litellm.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
    catalog = Path(".runtime/chat-models.json")
    if catalog.exists():
        catalog.chmod(0o600)
    catalog.write_text(json.dumps(chat_models, ensure_ascii=False, indent=2))
    catalog.chmod(0o444)
    override = {"services": {}}
    if tls == "nginx":
        override["services"]["web"] = {"ports": [f"{bind}:{cfg.get('APP_TLS_PORT', '8443')}:443"], "volumes": ["./deploy/nginx/tls.conf:/etc/nginx/conf.d/default.conf:ro", "./.secrets/tls:/run/tls:ro"]}
    Path(".runtime/compose.override.json").write_text(json.dumps(override))
    Path(".runtime/deploy-timeout").write_text(cfg.get("DEPLOY_TIMEOUT", "1800"))
    Path(env_file).chmod(0o600)
    print("Configuration validated; internal credentials preserved and runtime files prepared.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--infrastructure-only", action="store_true", help="Development preflight only; permits absent provider keys, never fabricates AI results")
    args = parser.parse_args()
    try:
        configure(args.env_file, args.check, args.infrastructure_only)
    except (ValueError, OSError):
        # Configuration errors name fields but never print the input values.
        import sys
        error = sys.exception()
        print(str(error) if isinstance(error, ValueError) else "Cannot read/write deployment configuration files", file=sys.stderr)
        sys.exit(1)
