"""Non-LLM extraction orchestration with stable requests and bounded reconciliation."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError

with workflow.unsafe.imports_passed_through():
    from app.workflows import material_activities as activities


OPTIONS = {"start_to_close_timeout": timedelta(seconds=40),
           "retry_policy": RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))}


@workflow.defn
class MaterialParseWorkflow:
    def __init__(self):
        self.cancel_requested = False

    @workflow.signal
    def cancel(self):
        self.cancel_requested = True

    @workflow.run
    async def run(self, operation_id: str) -> dict:
        try:
            state = await workflow.execute_activity(activities.prepare_material, operation_id, **OPTIONS)
            deadline = workflow.now() + timedelta(seconds=min(240, state["remaining_seconds"]) + 60)
            while not state["terminal"]:
                try:
                    if self.cancel_requested or state["cancel_requested"] or state["remaining_seconds"] <= 0:
                        state = await workflow.execute_activity(activities.cancel_material, operation_id, **OPTIONS)
                    elif not state["submitted"]:
                        state = await workflow.execute_activity(activities.submit_material, operation_id, **OPTIONS)
                    if state["terminal"]:
                        return state
                    if state["phase"] == "finished" and not state["result_saved"]:
                        state = await workflow.execute_activity(activities.collect_material, operation_id, **OPTIONS)
                    if state["needs_ack"]:
                        state = await workflow.execute_activity(activities.ack_material, operation_id, **OPTIONS)
                    if state["terminal"]:
                        return state
                    if workflow.now() >= deadline:
                        return await workflow.execute_activity(activities.fail_material,
                            {"operation_id": operation_id, "code": "deadline_exceeded"}, **OPTIONS)
                    await workflow.sleep(timedelta(seconds=2))
                    state = await workflow.execute_activity(activities.poll_material, operation_id, **OPTIONS)
                except ActivityError:
                    state = await workflow.execute_activity(activities.prepare_material, operation_id, **OPTIONS)
                    if not state["result_saved"]:
                        raise
                    # Saving is durable even if ACK is unavailable; never submit again.
                    if workflow.now() >= deadline:
                        return await workflow.execute_activity(activities.fail_material,
                            {"operation_id": operation_id, "code": "service_unavailable"}, **OPTIONS)
                    await workflow.sleep(timedelta(seconds=2))
            return state
        except (CancelledError, asyncio.CancelledError):
            try:
                await workflow.execute_activity(activities.cancel_material, operation_id, **OPTIONS)
            except Exception:
                pass
            raise
        except Exception:
            # Provider/transport errors can contain request bodies; never log their text or chains.
            workflow.logger.warning("Material parse %s interrupted", operation_id)
            try:
                saved = await workflow.execute_activity(activities.prepare_material, operation_id, **OPTIONS)
                if saved["submitted"] and not saved["cleanup_done"]:
                    await workflow.execute_activity(activities.cancel_material, operation_id, **OPTIONS)
            except Exception:
                pass
            return await workflow.execute_activity(activities.fail_material,
                {"operation_id": operation_id, "code": "service_unavailable"}, **OPTIONS)
