"""Replay-safe sample verification: one fresh environment per case, cleanup is independent."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, CancelledError

with workflow.unsafe.imports_passed_through():
    from app.workflows import verification_activities as activities

OPTIONS = {"start_to_close_timeout": timedelta(seconds=150),
           "retry_policy": RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))}
FINAL = {"passed", "failed", "inconclusive", "cancelled", "superseded", "expired"}


@workflow.defn
class VerificationWorkflow:
    def __init__(self):
        self.cancel_requested = False

    @workflow.signal
    def cancel(self):
        self.cancel_requested = True

    async def _run(self, operation_id):
        state = await workflow.execute_activity(activities.prepare, operation_id, **OPTIONS)
        cleanup_deadline = workflow.now() + timedelta(seconds=state["remaining_seconds"] + 86400)
        while not state["terminal"]:
            if ((self.cancel_requested or state["cancel_requested"] or state["remaining_seconds"] <= 0)
                    and state["status"] not in FINAL):
                state = await workflow.execute_activity(activities.stop, {"operation_id": operation_id,
                    "expired": state["remaining_seconds"] <= 0}, **OPTIONS)
            try:
                state = await workflow.execute_activity(activities.advance, operation_id, **OPTIONS)
            except ActivityError:
                # Fail only the unfinished cases. Saved receipts and cleanup targets survive.
                state = await workflow.execute_activity(activities.fail, operation_id, **OPTIONS)
            if state["terminal"]:
                return state
            if workflow.now() >= cleanup_deadline:
                return state  # Explicit cleanup_done=false remains persisted and blocks new work.
            await workflow.sleep(timedelta(seconds=10 if state["status"] in FINAL else 2))
        return state

    @workflow.run
    async def run(self, operation_id: str) -> dict:
        try:
            return await self._run(operation_id)
        except (CancelledError, asyncio.CancelledError):
            # Normal UI cancellation is a signal. External workflow cancellation still
            # drains existing environments; the node TTL is an additional independent cap.
            task = asyncio.create_task(self._cancel_and_clean(operation_id))
            await asyncio.shield(task)
            raise

    async def _cancel_and_clean(self, operation_id):
        state = await workflow.execute_activity(activities.stop, {"operation_id": operation_id}, **OPTIONS)
        for _ in range(12):
            if state["terminal"]:
                return state
            try:
                state = await workflow.execute_activity(activities.advance, operation_id, **OPTIONS)
            except ActivityError:
                state = await workflow.execute_activity(activities.fail, operation_id, **OPTIONS)
        return state
