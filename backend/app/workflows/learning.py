import asyncio
import json
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import CancelledError, FailureError

with workflow.unsafe.imports_passed_through():
    from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    from pydantic_ai.usage import UsageLimits

    from app.agents.learning import AGENTS, build_prompt
    from app.workflows import activities


DB_OPTIONS = {"start_to_close_timeout": timedelta(seconds=30),
              "retry_policy": RetryPolicy(maximum_attempts=5)}


@workflow.defn
class OperationWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = tuple(AGENTS.values())

    @workflow.run
    async def run(self, operation_id: str) -> dict:
        try:
            context = await workflow.execute_activity(activities.load_operation, operation_id, **DB_OPTIONS)
            if context.get("operation", {}).get("status") in {"succeeded", "cancelled", "superseded"}:
                return {"status": context["operation"]["status"]}
            await workflow.execute_activity(activities.set_stage, {
                "operation_id": operation_id, "stage": "reasoning"}, **DB_OPTIONS)
            agent = AGENTS[context["operation"]["kind"]]
            history = ModelMessagesTypeAdapter.validate_python(context.get("agent_history") or [])
            result = await agent.run(
                build_prompt(context), deps={"operation": {"id": operation_id}}, message_history=history,
                model_settings={"learning_operation_id": operation_id},
                usage_limits=UsageLimits(request_limit=6, total_tokens_limit=80000),
            )
            usage = result.usage
            await workflow.execute_activity(activities.save_usage, {
                "operation_id": operation_id, "usage": {"requests": usage.requests,
                    "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}}, **DB_OPTIONS)
            proposal = result.output.model_dump(mode="json")
            proposal["agent_history"] = json.loads(result.new_messages_json())
            await workflow.execute_activity(activities.set_stage, {
                "operation_id": operation_id, "stage": "validating"}, **DB_OPTIONS)
            return await workflow.execute_activity(activities.commit_proposal, {
                "operation_id": operation_id, "result": proposal}, **DB_OPTIONS)
        except (CancelledError, asyncio.CancelledError):
            raise
        except Exception as error:  # noqa: BLE001 -- durable boundary persists only a redacted failure
            # Do not persist provider exceptions: they may contain credentials or user payloads.
            workflow.logger.warning("Operation %s failed: %s", operation_id, type(error).__name__)
            return await workflow.execute_activity(activities.fail_operation, {
                "operation_id": operation_id, "code": "agent_failed",
                "message": "本次处理未完成，请检查模型服务配置或稍后重试。"}, **DB_OPTIONS)


@workflow.defn
class GoalWorkflow:
    def __init__(self):
        self.pending: list[dict] = []
        self.seen: list[str] = []
        self.changed = False
        self.closed = False
        self.current = None
        self.current_operation_id = None
        self.child_cancel_requested = False

    @workflow.signal
    async def command(self, data: dict):
        command_id = data.get("command_id", data.get("operation_id", ""))
        if command_id and command_id in self.seen:
            return
        if command_id:
            self.seen.append(command_id)
        kind = data.get("type", "")
        if kind in {"goal_cancelled", "goal_completed"}:
            self.closed = True
        if kind in {"goal_cancelled", "goal_paused", "goal_completed", "operation_cancelled"}:
            if self.current and (kind != "operation_cancelled" or
                                 data.get("operation_id") == self.current_operation_id):
                self.child_cancel_requested = True
                self.current.cancel()
        elif data.get("operation_id") and kind in {
            "goal_created", "user_message", "submission_created", "recommendation_requested",
        }:
            self.pending.append(data)
        self.changed = True

    @workflow.run
    async def run(self, state: dict):
        self.pending.extend(state.get("pending", []))
        self.seen.extend(state.get("seen", []))
        for _ in range(500):
            if self.closed:
                return
            self.changed = False
            while self.pending and not self.closed:
                command = self.pending.pop(0)
                try:
                    self.current_operation_id = command["operation_id"]
                    self.child_cancel_requested = False
                    self.current = await workflow.start_child_workflow(
                        OperationWorkflow.run, command["operation_id"],
                        id=f"operation-{command['operation_id']}",
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        execution_timeout=timedelta(minutes=15),
                    )
                    await self.current
                except asyncio.CancelledError:
                    if not self.child_cancel_requested:
                        raise
                    await workflow.execute_activity(activities.fail_operation, {
                        "operation_id": command["operation_id"], "code": "interrupted",
                        "message": "处理已中断，可在恢复目标后重试。"}, **DB_OPTIONS)
                except FailureError as error:
                    # A cancelled/duplicate child cannot bypass the database's publication fence.
                    workflow.logger.warning("Operation child ended: %s", type(error).__name__)
                    await workflow.execute_activity(activities.fail_operation, {
                        "operation_id": command["operation_id"], "code": "interrupted",
                        "message": "处理已中断，可稍后重试。"}, **DB_OPTIONS)
                finally:
                    self.current = None
                    self.current_operation_id = None
            if self.closed:
                return
            try:
                tick = await workflow.execute_activity(activities.proactive_tick, {
                    "user_id": state["user_id"], "goal_id": state["goal_id"]}, **DB_OPTIONS)
            except FailureError:
                workflow.logger.warning("Goal follow-up temporarily unavailable")
                tick = {"next_check_seconds": 60}
            if tick.get("status") in {"cancelled", "completed", "closed"}:
                return
            if tick.get("operation_id"):
                self.pending.append(tick)
                continue
            try:
                await workflow.wait_condition(lambda: self.changed or self.closed,
                                              timeout=timedelta(seconds=tick.get("next_check_seconds", 3600)))
            except TimeoutError:
                pass
        workflow.continue_as_new({"user_id": state["user_id"], "goal_id": state["goal_id"],
                                  "pending": self.pending, "seen": self.seen[-1000:]})


@workflow.defn
class MemorySyncWorkflow:
    @workflow.run
    async def run(self, data: dict) -> dict:
        try:
            return await workflow.execute_activity(activities.memory_sync, data["source_id"],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=10, initial_interval=timedelta(seconds=10),
                                         maximum_interval=timedelta(minutes=10)))
        except FailureError:
            return await workflow.execute_activity(activities.memory_failed, data, **DB_OPTIONS)


@workflow.defn
class SmokeWorkflow:
    @workflow.run
    async def run(self, value: str) -> str:
        await workflow.sleep(timedelta(milliseconds=100))
        return await workflow.execute_activity(activities.smoke_activity, value, **DB_OPTIONS)


@workflow.defn
class MaintenanceWorkflow:
    @workflow.run
    async def run(self):
        for _ in range(30):
            try:
                await workflow.execute_activity(activities.maintenance, **DB_OPTIONS)
            except FailureError:
                workflow.logger.warning("Expired-record cleanup will retry on its next scheduled run")
            await workflow.sleep(timedelta(days=1))
        workflow.continue_as_new()


@workflow.defn
class AssistantWorkflow:
    """One durable serial inbox for personal conversations without a learning goal."""

    def __init__(self):
        self.pending: list[str] = []
        self.seen: list[str] = []
        self.current = None
        self.operation_id = None
        self.cancel_requested = False

    @workflow.signal
    async def command(self, data: dict):
        if data.get("type") == "operation_cancelled":
            if self.current and data.get("operation_id") == self.operation_id:
                self.cancel_requested = True
                self.current.cancel()
            return
        command_id = data["command_id"]
        if command_id not in self.seen:
            self.seen.append(command_id)
            self.pending.append(data["operation_id"])

    @workflow.run
    async def run(self, state: dict):
        self.pending.extend(state.get("pending", []))
        self.seen.extend(state.get("seen", []))
        for _ in range(500):
            try:
                await workflow.wait_condition(lambda: bool(self.pending), timeout=timedelta(days=7))
            except TimeoutError:
                continue
            self.operation_id = self.pending.pop(0)
            self.cancel_requested = False
            interrupted = False
            try:
                self.current = await workflow.start_child_workflow(OperationWorkflow.run, self.operation_id,
                    id=f"operation-{self.operation_id}", id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    execution_timeout=timedelta(minutes=15))
                await self.current
            except asyncio.CancelledError:
                if not self.cancel_requested:
                    raise
                interrupted = True
            except FailureError:
                interrupted = True
            finally:
                self.current = None
            if interrupted:
                await workflow.execute_activity(activities.fail_operation, {"operation_id": self.operation_id,
                    "code": "interrupted", "message": "本次回复已中断，可以重试。"}, **DB_OPTIONS)
        workflow.continue_as_new({"user_id": state["user_id"], "pending": self.pending,
                                  "seen": self.seen[-1000:]})
