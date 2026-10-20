"""Read only this service's mounted secrets, then run application without root."""
import os
import sys
from pathlib import Path
from urllib.parse import quote

password = Path("/run/secrets/app_db_password").read_text().strip()
os.environ["APP_DATABASE_URL"] = f"postgresql+asyncpg://app:{quote(password, safe='')}@postgres-core:5432/app"
os.environ["DATABASE_URL"] = os.environ["APP_DATABASE_URL"]
for name, path in {
    "LITELLM_API_KEY": "/run/service-keys/app.key",
    "BOOTSTRAP_PASSWORD": "/run/secrets/bootstrap_password",
}.items():
    if Path(path).exists():
        os.environ[name] = Path(path).read_text().strip()
if os.getuid() == 0:
    directory = Path(os.environ.get("APP_ATTACHMENT_DIR", "/data/attachments"))
    directory.mkdir(parents=True, exist_ok=True)
    os.chown(directory, 10001, 10001)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
    os.environ["HOME"] = "/home/app"
os.execvp(sys.argv[1], sys.argv[1:])
