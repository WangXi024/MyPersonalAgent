import os
from pathlib import Path
from urllib.parse import quote

for name in ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "BAILIAN_API_KEY", "ZHIPU_API_KEY", "ARK_API_KEY"):
    os.environ[name] = Path("/run/secrets", name.lower()).read_text().strip()
password = Path("/run/secrets/litellm_db_password").read_text().strip()
os.environ["DATABASE_URL"] = f"postgresql://litellm:{quote(password, safe='')}@postgres-core:5432/litellm"
os.execv("/app/docker/prod_entrypoint.sh", ["/app/docker/prod_entrypoint.sh", "--config", "/etc/litellm/config.json", "--host", "0.0.0.0", "--port", "4000"])
