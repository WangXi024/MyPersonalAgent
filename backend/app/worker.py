import asyncio
import logging

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from app.integrations.settings import runtime_settings
from app.workflows.activities import ACTIVITIES
from app.workflows.coding import CodingWorkflow
from app.workflows.coding_activities import CODING_ACTIVITIES
from app.workflows.verification import VerificationWorkflow
from app.workflows.verification_activities import VERIFICATION_ACTIVITIES
from app.workflows.materials import MaterialParseWorkflow
from app.workflows.material_activities import MATERIAL_ACTIVITIES
from app.workflows.sandbox import SandboxExecutionWorkflow
from app.workflows.sandbox_activities import SANDBOX_ACTIVITIES
from app.workflows.learning import (AssistantWorkflow, GoalWorkflow, MaintenanceWorkflow,
                                    MemorySyncWorkflow, OperationWorkflow, SmokeWorkflow)


async def main():
    settings = runtime_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace,
                                  plugins=[PydanticAIPlugin()])
    worker = Worker(client, task_queue=settings.temporal_task_queue,
                    workflows=[GoalWorkflow, OperationWorkflow, MemorySyncWorkflow, SmokeWorkflow,
                               MaintenanceWorkflow, AssistantWorkflow, SandboxExecutionWorkflow, CodingWorkflow,
                               VerificationWorkflow, MaterialParseWorkflow],
                    activities=ACTIVITIES + SANDBOX_ACTIVITIES + CODING_ACTIVITIES + VERIFICATION_ACTIVITIES
                               + MATERIAL_ACTIVITIES, max_concurrent_activities=10)
    await worker.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
