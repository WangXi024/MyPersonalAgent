from urllib.parse import quote

import httpx

from app.integrations.settings import runtime_settings


class HindsightClient:
    def __init__(self, transport=None):
        settings = runtime_settings()
        key = settings.hindsight_api_key.get_secret_value()
        self.client = httpx.AsyncClient(
            base_url=settings.hindsight_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=httpx.Timeout(180, connect=5), transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    @staticmethod
    def bank_path(bank_id: str) -> str:
        return f"/v1/default/banks/{quote(bank_id, safe='')}"

    async def retain(self, source: dict, document_id: str):
        response = await self.client.post(self.bank_path(source["bank_id"]) + "/memories", json={
            "items": [{"content": source["content"], "document_id": document_id,
                       "metadata": {"source_id": source["id"], "version": str(source["version"])},
                       "tags": ["learning", f"source:{source['id']}"]}], "async": False,
        })
        response.raise_for_status()

    async def delete(self, bank_id: str, document_id: str):
        response = await self.client.delete(self.bank_path(bank_id) + "/documents/" + quote(document_id, safe=""))
        if response.status_code != 404:
            response.raise_for_status()

    async def recall(self, bank_id: str, query: str) -> list[dict]:
        response = await self.client.post(self.bank_path(bank_id) + "/memories/recall", json={
            "query": query, "budget": "low", "max_tokens": 1500, "types": ["world", "experience"],
        }, timeout=8)
        response.raise_for_status()
        return response.json().get("results", [])


def filter_recalled_memories(results: list[dict], sources: list[dict]) -> list[dict]:
    valid = {str(s["id"]): str(s["version"]) for s in sources
             if not s.get("suppressed") and not s.get("deleted")}
    accepted = []
    for result in results:
        meta = result.get("metadata") or {}
        source_id = str(meta.get("source_id", ""))
        if result.get("type") not in {"world", "experience"}:
            continue
        if source_id not in valid or str(meta.get("version")) != valid[source_id]:
            continue
        accepted.append({"text": str(result.get("text", ""))[:2000], "source_id": source_id,
                         "version": valid[source_id]})
    return accepted[:10]


async def recall_for_operation(operation_id: str, query: str) -> dict:
    from app.domain.execution import get_memory_recall_scope

    context = await get_memory_recall_scope(operation_id)
    if not context["enabled"]:
        return {"status": "unavailable", "memories": []}
    try:
        async with HindsightClient() as client:
            results = await client.recall(context["bank_id"], query)
        # Recheck the local tombstones after I/O; never admit facts without live source provenance.
        source_ids = [str((item.get("metadata") or {}).get("source_id", "")) for item in results]
        current = await get_memory_recall_scope(operation_id, source_ids)
        if not current["enabled"] or current["memory_control_revision"] != context["memory_control_revision"]:
            return {"status": "changed", "memories": []}
        return {"status": "ok", "memories": filter_recalled_memories(results, current["sources"])}
    except (httpx.HTTPError, KeyError):
        return {"status": "unavailable", "memories": []}


async def sync_memory(source_id: str) -> dict:
    from app.domain.execution import ack_memory_sync, load_memory_source

    source = await load_memory_source(source_id)
    if not source:
        return {"status": "missing"}
    source["id"] = source_id
    version = source["version"]
    document_id = f"{source_id}:v{version}"
    async with HindsightClient() as client:
        if not source["suppressed"] and not source["deleted"]:
            await client.retain(source, document_id)
        current = await load_memory_source(source_id)
        stale = current["version"] != version or current["suppressed"] or current["deleted"]
        # Versioned documents make a late write harmless to newer content. Remove its obsolete source.
        for previous in range(1, version + (1 if stale else 0)):
            await client.delete(source["bank_id"], f"{source_id}:v{previous}")
        if stale and not (current["suppressed"] or current["deleted"]):
            return {"status": "superseded"}
        return await ack_memory_sync(source_id, version, document_id if not stale else None)
