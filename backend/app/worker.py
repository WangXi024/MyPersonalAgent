import asyncio
import logging

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from app.integrations.settings import runtime_settings
from app.workflows.activities import ACTIVITIES
from app.workflows.learning import (AssistantWorkflow, GoalWorkflow, MaintenanceWorkflow,
                                    MemorySyncWorkflow, OperationWorkflow, SmokeWorkflow)


async def main():
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace,
                                  plugins=[PydanticAIPlugin()])
    worker = Worker(client, task_queue=settings.temporal_task_queue,
                    workflows=[GoalWorkflow, OperationWorkflow, MemorySyncWorkflow, SmokeWorkflow,
                               MaintenanceWorkflow, AssistantWorkflow],
                    activities=ACTIVITIES, max_concurrent_activities=10)
    await worker.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
