"""Small activity results only: code and artifact bytes never enter workflow history."""

from temporalio import activity
from temporalio.exceptions import ApplicationError


async def _call(name: str, operation_id: str, *args):
    from app.domain import sandbox_execution
    from app.domain.common import DomainError

    try:
        return await getattr(sandbox_execution, name)(operation_id, *args)
    except DomainError as error:
        raise ApplicationError(error.message, type=error.code, non_retryable=True) from None


@activity.defn(name="sandbox_prepare_execution")
async def prepare_execution(operation_id: str) -> dict:
    return await _call("prepare_execution", operation_id)


@activity.defn(name="sandbox_submit_execution")
async def submit_execution(operation_id: str) -> dict:
    return await _call("submit_execution", operation_id)


@activity.defn(name="sandbox_poll_execution")
async def poll_execution(operation_id: str) -> dict:
    return await _call("poll_execution", operation_id)


@activity.defn(name="sandbox_collect_execution")
async def collect_execution(operation_id: str) -> dict:
    return await _call("collect_execution", operation_id)


@activity.defn(name="sandbox_ack_execution")
async def ack_execution(operation_id: str) -> dict:
    return await _call("ack_execution", operation_id)


@activity.defn(name="sandbox_cancel_execution")
async def cancel_execution(operation_id: str) -> dict:
    return await _call("cancel_execution", operation_id)


@activity.defn(name="sandbox_fail_execution")
async def fail_execution(data: dict) -> dict:
    return await _call("fail_execution", data["operation_id"], data["code"])


SANDBOX_ACTIVITIES = [prepare_execution, submit_execution, poll_execution, collect_execution,
                      ack_execution, cancel_execution, fail_execution]
