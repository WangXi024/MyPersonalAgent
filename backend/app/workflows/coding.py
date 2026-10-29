"""One official Coder run, one scoped workspace, durable export followed by destruction."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError

with workflow.unsafe.imports_passed_through():
    from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
    from pydantic_ai.usage import UsageLimits

    from app.agents.coder import CODER_AGENT
    from app.workflows import coding_activities as activities


OPTIONS = {"start_to_close_timeout": timedelta(seconds=180),
           "retry_policy": RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))}


@workflow.defn
class CodingWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [CODER_AGENT]

    def __init__(self):
        self.cancel_requested = False

    @workflow.signal
    def cancel(self):
        self.cancel_requested = True

    async def _finish(self, operation_id, *, summary="", error_code=None):
        # A lost Close acknowledgement must never restart the model or create a replacement
        # workspace. Keep retrying only the idempotent export/close lifecycle.
        cleanup_deadline = workflow.now() + timedelta(hours=24)
        state = {"operation_id": operation_id, "terminal": False, "cleanup_done": False}
        while True:
            try:
                if self.cancel_requested:
                    state = await workflow.execute_activity(activities.cancel_coding, operation_id, **OPTIONS)
                else:
                    state = await workflow.execute_activity(activities.finish_coding,
                        {"operation_id": operation_id, "summary": summary, "error_code": error_code}, **OPTIONS)
                if state["terminal"]:
                    return state
            except ActivityError:
                workflow.logger.warning("Coder lifecycle pending for operation %s", operation_id)
            if workflow.now() >= cleanup_deadline:
                return state
            await workflow.sleep(timedelta(seconds=30))

    @workflow.run
    async def run(self, operation_id: str) -> dict:
        agent_task = None
        summary, error_code = "", None
        try:
            state = await workflow.execute_activity(activities.prepare_coding, operation_id, **OPTIONS)
            if state["terminal"]:
                return state
            self.cancel_requested = self.cancel_requested or state["cancel_requested"]
            if state.get("agent_ready") and not self.cancel_requested:
                agent_task = asyncio.create_task(CODER_AGENT.run(state["prompt"], deps=state["deps"],
                    model_settings={"learning_operation_id": operation_id},
                    usage_limits=UsageLimits(request_limit=6, total_tokens_limit=80000)))
                try:
                    await workflow.wait_condition(lambda: self.cancel_requested or agent_task.done(),
                        timeout=timedelta(seconds=max(0, state["remaining_seconds"])))
                except TimeoutError:
                    error_code = "operation_timeout"
                if not agent_task.done():
                    agent_task.cancel()
                    await asyncio.gather(agent_task, return_exceptions=True)
                elif not self.cancel_requested:
                    summary = agent_task.result().output
        except (CancelledError, asyncio.CancelledError):
            self.cancel_requested = True
            if agent_task and not agent_task.done():
                agent_task.cancel()
                await asyncio.gather(agent_task, return_exceptions=True)
            # Public cancellation is a signal; this branch also drains an external Temporal
            # cancellation. The node's hard TTL remains a second cleanup boundary.
            await asyncio.shield(self._finish(operation_id))
            raise
        except Exception as error:  # noqa: BLE001 -- persist no provider payload or credentials
            workflow.logger.warning("Coder operation %s failed: %s", operation_id, type(error).__name__)
            error_code = "agent_failed"
        return await self._finish(operation_id, summary=summary, error_code=error_code)
