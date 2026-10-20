"""No model calls: prove worker registration, durable timer and activity execution."""
import asyncio
from datetime import timedelta
from uuid import uuid4

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client

from app.integrations.settings import runtime_settings
from app.workflows.learning import SmokeWorkflow


async def main():
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace,
                                  plugins=[PydanticAIPlugin()])
    result = await client.execute_workflow(SmokeWorkflow.run, "ready", id=f"smoke-{uuid4()}",
        task_queue=settings.temporal_task_queue, execution_timeout=timedelta(seconds=45))
    if result != "ready":
        raise RuntimeError("Temporal smoke did not return the expected result")
    print("Temporal worker, durable timer and activity: OK (no model call)")


if __name__ == "__main__":
    asyncio.run(main())
