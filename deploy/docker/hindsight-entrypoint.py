import os
import sys
import json
from pathlib import Path
from urllib.parse import quote

password = Path("/run/secrets/memory_db_password").read_text().strip()
os.environ["HINDSIGHT_API_DATABASE_URL"] = f"postgresql://hindsight:{quote(password, safe='')}@postgres-memory:5432/hindsight"
key = Path("/run/service-keys/memory.key")
if key.exists():
    os.environ["HINDSIGHT_API_LLM_API_KEY"] = key.read_text().strip()
if os.getuid() == 0:
    Path("/models").mkdir(exist_ok=True)
    os.chown("/models", 1000, 1000)
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
if sys.argv[1] == "migrate":
    os.execvp("hindsight-admin", ["hindsight-admin", "run-db-migration"])
from huggingface_hub import snapshot_download

for variable, model, revision in (
    ("HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL", "intfloat/multilingual-e5-small", "614241f622f53c4eeff9890bdc4f31cfecc418b3"),
    ("HINDSIGHT_API_RERANKER_LOCAL_MODEL", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1", "1427fd652930e4ba29e8149678df786c240d8825"),
):
    print("Preparing pinned local multilingual model: " + model, flush=True)
    snapshot = snapshot_download(model, revision=revision, cache_dir="/models", ignore_patterns=["onnx/**", "openvino/**", "*.h5", "*.msgpack"])
    if variable == "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL":
        # Hindsight's local provider uses SentenceTransformers prompts, not the
        # remote-provider prefix env vars. The pinned E5 snapshot has no prompts.
        # Keep downloaded weights immutable and add only a local prompt config.
        configured = Path("/models", "learning-e5-" + revision)
        configured.mkdir(exist_ok=True)
        for source in Path(snapshot).iterdir():
            target = configured / source.name
            if source.name != "config_sentence_transformers.json" and not target.exists():
                target.symlink_to(source, target_is_directory=source.is_dir())
        (configured / "config_sentence_transformers.json").write_text(json.dumps({"prompts": {"query": "query: ", "document": "passage: "}, "default_prompt_name": None}))
        snapshot = str(configured)
    os.environ[variable] = snapshot
os.execvp("hindsight-api", ["hindsight-api"])
