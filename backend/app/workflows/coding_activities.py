"""Small lifecycle inputs; source loading and file export run outside workflow code.

Coder tool arguments/results can still include code in the Agent's durable history.
"""

from temporalio import activity
from temporalio.exceptions import ApplicationError


async def _call(name, operation_id, **kwargs):
    from app.domain import coding_runs
    from app.domain.common import DomainError

    try:
        return await getattr(coding_runs, name)(operation_id, **kwargs)
    except DomainError as error:
        raise ApplicationError(error.message, type=error.code, non_retryable=True) from None


@activity.defn(name="coding_prepare")
async def prepare_coding(operation_id: str) -> dict:
    return await _call("prepare_coding", operation_id)


@activity.defn(name="coding_finish")
async def finish_coding(data: dict) -> dict:
    return await _call("finish_coding", data["operation_id"], summary=data.get("summary", ""),
                       error_code=data.get("error_code"))


@activity.defn(name="coding_cancel")
async def cancel_coding(operation_id: str) -> dict:
    return await _call("cancel_coding", operation_id)


CODING_ACTIVITIES = [prepare_coding, finish_coding, cancel_coding]
