from temporalio import activity
from temporalio.exceptions import ApplicationError


@activity.defn
async def load_operation(operation_id: str) -> dict:
    from app.domain.execution import load_context
    from app.integrations.settings import runtime_settings

    if not runtime_settings().litellm_api_key.get_secret_value():
        raise ApplicationError("模型网关尚未配置", type="ConfigurationError", non_retryable=True)
    return await load_context(operation_id)


@activity.defn
async def set_stage(data: dict) -> dict:
    from app.domain.execution import mark_running
    return await mark_running(data["operation_id"], data["stage"])


@activity.defn
async def commit_proposal(data: dict) -> dict:
    from app.domain.execution import commit_result
    return await commit_result(data["operation_id"], data["result"])


@activity.defn
async def save_usage(data: dict) -> dict:
    from app.domain.execution import record_model_usage
    return await record_model_usage(data["operation_id"], data["usage"])


@activity.defn
async def fail_operation(data: dict) -> dict:
    from app.domain.execution import mark_failed
    return await mark_failed(data["operation_id"], data["code"], data["message"])


@activity.defn
async def proactive_tick(data: dict) -> dict:
    from app.domain.execution import prepare_proactive
    return await prepare_proactive(data["user_id"], data["goal_id"])


@activity.defn
async def memory_sync(source_id: str) -> dict:
    from app.integrations.hindsight import sync_memory
    return await sync_memory(source_id)


@activity.defn
async def memory_failed(data: dict) -> dict:
    from app.domain.execution import mark_memory_failed
    return await mark_memory_failed(data["source_id"], data["version"])


@activity.defn
async def smoke_activity(value: str) -> str:
    return value


@activity.defn
async def maintenance() -> dict:
    from app.integrations.maintenance import prune_expired_records
    return await prune_expired_records()


ACTIVITIES = [load_operation, set_stage, commit_proposal, save_usage, fail_operation,
              proactive_tick, memory_sync, memory_failed, smoke_activity, maintenance]
