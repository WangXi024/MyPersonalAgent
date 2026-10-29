"""Durable orchestration for explicitly requested sandbox executions."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError

with workflow.unsafe.imports_passed_through():
    from app.workflows import sandbox_activities as activities


OPTIONS = {
    "start_to_close_timeout": timedelta(seconds=45),
    "retry_policy": RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1)),
}


@workflow.defn
class SandboxExecutionWorkflow:
    def __init__(self):
        self.cancel_requested = False

    @workflow.signal
    def cancel(self):
        self.cancel_requested = True

    @workflow.run
    async def run(self, operation_id: str) -> dict:
        state = {}
        try:
            state = await workflow.execute_activity(activities.prepare_execution, operation_id, **OPTIONS)
            if state["terminal"]:
                return state
            deadline = workflow.now() + timedelta(seconds=max(0, state["remaining_seconds"]) + 60)
            if not state["submitted"]:
                state = await workflow.execute_activity(
                    activities.cancel_execution if state["cancel_requested"] or self.cancel_requested
                    or state["remaining_seconds"] <= 0
                    else activities.submit_execution, operation_id, **OPTIONS)
            while not state["terminal"]:
                try:
                    if self.cancel_requested or state["cancel_requested"] or state["remaining_seconds"] <= 0:
                        state = await workflow.execute_activity(activities.cancel_execution, operation_id, **OPTIONS)
                    # Result bodies expire quickly; collect immediately, track cleanup separately.
                    if state["phase"] == "finished" and not state["result_saved"]:
                        state = await workflow.execute_activity(activities.collect_execution, operation_id, **OPTIONS)
                    if state["needs_ack"]:
                        state = await workflow.execute_activity(activities.ack_execution, operation_id, **OPTIONS)
                    if not state["terminal"]:
                        await workflow.sleep(timedelta(seconds=2))
                        state = await workflow.execute_activity(activities.poll_execution, operation_id, **OPTIONS)
                except ActivityError:
                    saved = await workflow.execute_activity(activities.prepare_execution, operation_id, **OPTIONS)
                    if not saved["result_saved"]:
                        raise
                    # A saved result is never re-executed because its ACK transport failed.
                    state = saved
                    await workflow.sleep(timedelta(seconds=2))
                if state["terminal"]:
                    return state
                if workflow.now() >= deadline:
                    return await workflow.execute_activity(activities.fail_execution,
                        {"operation_id": operation_id, "code": "deadline_exceeded"}, **OPTIONS)
            return state
        except (CancelledError, asyncio.CancelledError):
            # The public API uses a signal/DB flag so ordinary cancellation drains above.
            # External Temporal cancellation still requests sandbox cleanup before exiting.
            try:
                await workflow.execute_activity(activities.cancel_execution, operation_id, **OPTIONS)
            except Exception:
                pass
            raise
        except Exception:
            workflow.logger.warning("Sandbox execution %s failed", operation_id)
            try:
                await workflow.execute_activity(activities.cancel_execution, operation_id, **OPTIONS)
            except Exception:
                pass
            return await workflow.execute_activity(activities.fail_execution,
                {"operation_id": operation_id, "code": "service_unavailable"}, **OPTIONS)
