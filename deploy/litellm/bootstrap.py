"""Idempotent restricted virtual keys. Never expose keys in errors or stdout."""
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

master = Path("/run/secrets/litellm_master_key").read_text().strip()
base = "http://litellm:4000"


def request(path, body, key=master):
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


try:
    directory = Path("/keys")
    directory.mkdir(exist_ok=True)
    for service, env in (("app", "APP_MODEL_MONTHLY_BUDGET"), ("memory", "MEMORY_MODEL_MONTHLY_BUDGET")):
        path = directory / (service + ".key")
        if path.exists():
            key = path.read_text().strip()
        else:
            key = "sk-" + secrets.token_hex(32)
            # Persist before the remote request so a crash reuses the same key.
            path.write_text(key)
            path.chmod(0o444)
        headers = {"Authorization": "Bearer " + key}
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/v1/models", headers=headers), timeout=30):
                exists = True
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise
            exists = False
        models = ["learning-agent", "learning-fallback"]
        if service == "app":
            models.append("learning-configured")
        body = {"key": key, "key_alias": "learning-" + service, "models": models, "max_budget": float(os.environ[env]), "budget_duration": "30d", "rpm_limit": 30, "tpm_limit": 100000, "metadata": {"service": service}}
        request("/key/update" if exists else "/key/generate", body)
    print("Restricted application and memory gateway keys are ready.")
except Exception:
    print("Gateway key initialization failed; check gateway readiness and configuration.", file=sys.stderr)
    sys.exit(1)
