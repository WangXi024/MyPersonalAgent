import asyncio
import logging
import os
from uuid import uuid4

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app.domain.execution import ack_outbox, claim_outbox, get_dispatch_state, retry_outbox
from app.integrations.settings import runtime_settings
from app.workflows.learning import AssistantWorkflow, GoalWorkflow, MaintenanceWorkflow, MemorySyncWorkflow

logger = logging.getLogger(__name__)
CONTROL_COMMANDS = {"goal_paused", "goal_resumed", "goal_cancelled", "goal_completed",
                    "operation_cancelled", "plan_confirmed", "availability_changed"}


class ConversationOrderPending(Exception):
    """A preceding saved message must finish before this message is dispatched."""


async def deliver(client: Client, item: dict):
    payload = dict(item["payload"])
    settings = runtime_settings()
    if item["kind"] == "memory":
        try:
            await client.start_workflow(MemorySyncWorkflow.run,
                {"source_id": payload["source_id"], "version": payload["version"]},
                id=f"memory-{payload['source_id']}-{item['id']}",
                task_queue=settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
        except WorkflowAlreadyStartedError:
            pass
        return
    state = await get_dispatch_state(payload)
    payload["command_id"] = item["id"]
    if not payload.get("goal_id"):
        if state["operation_status"] == "missing":
            return
        if payload["type"] != "operation_cancelled" and state["operation_status"] in {
            "succeeded", "failed", "superseded", "cancelled",
        }:
            return
        if payload["type"] != "operation_cancelled" and state.get("blocked_by_prior_operation"):
            raise ConversationOrderPending()
        workflow_id = f"assistant-{payload['user_id']}"
        try:
            await client.start_workflow(AssistantWorkflow.run, {"user_id": payload["user_id"]},
                id=workflow_id, task_queue=settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
        except WorkflowAlreadyStartedError:
            pass
        await client.get_workflow_handle(workflow_id).signal(AssistantWorkflow.command, payload)
        return
    workflow_id = f"goal-{payload['user_id']}-{payload['goal_id']}"
    if state["goal_status"] in {"missing", "cancelled", "completed"}:
        if payload["type"] in CONTROL_COMMANDS and state["goal_status"] != "missing":
            try:
                await client.get_workflow_handle(workflow_id).signal(GoalWorkflow.command, payload)
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
        return  # A closed goal never gets recreated by delayed outbox delivery.
    if payload["type"] not in CONTROL_COMMANDS and state["operation_status"] in {
        "missing", "succeeded", "failed", "superseded", "cancelled",
    }:
        return
    try:
        await client.start_workflow(GoalWorkflow.run,
            {"user_id": payload["user_id"], "goal_id": payload["goal_id"]},
            id=workflow_id, task_queue=settings.temporal_task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
    except WorkflowAlreadyStartedError:
        pass
    await client.get_workflow_handle(workflow_id).signal(GoalWorkflow.command, payload)


async def main():
    settings = runtime_settings()
    owner = f"dispatcher-{os.getpid()}-{uuid4().hex[:8]}"
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace,
                                  plugins=[PydanticAIPlugin()])
    try:
        await client.start_workflow(MaintenanceWorkflow.run, id="learning-maintenance-v1",
            task_queue=settings.temporal_task_queue, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
    except WorkflowAlreadyStartedError:
        pass
    while True:
        try:
            items = await claim_outbox(owner, 20)
            for item in items:
                try:
                    await deliver(client, item)
                    await ack_outbox(item["id"], owner)
                except Exception as error:  # noqa: BLE001 -- outbox must survive transient delivery failures
                    await retry_outbox(item["id"], owner, type(error).__name__)
            if not items:
                await asyncio.sleep(settings.dispatcher_poll_seconds)
        except Exception as error:  # noqa: BLE001 -- keep the dispatcher alive without logging payloads
            logger.warning("Outbox iteration failed: %s", type(error).__name__)
            await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
