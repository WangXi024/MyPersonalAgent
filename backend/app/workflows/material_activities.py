"""Only small metadata crosses Temporal; documents and extracted bodies stay outside it."""

from temporalio import activity
from temporalio.exceptions import ApplicationError


async def _call(name, operation_id, *args):
    from app.domain import materials
    from app.domain.common import DomainError
    try:
        return await getattr(materials, name)(operation_id, *args)
    except DomainError as error:
        raise ApplicationError(error.message, type=error.code, non_retryable=True) from None


@activity.defn(name="material_prepare")
async def prepare_material(operation_id: str) -> dict:
    return await _call("prepare_material", operation_id)


@activity.defn(name="material_submit")
async def submit_material(operation_id: str) -> dict:
    return await _call("submit_material", operation_id)


@activity.defn(name="material_poll")
async def poll_material(operation_id: str) -> dict:
    return await _call("poll_material", operation_id)


@activity.defn(name="material_collect")
async def collect_material(operation_id: str) -> dict:
    return await _call("collect_material", operation_id)


@activity.defn(name="material_ack")
async def ack_material(operation_id: str) -> dict:
    return await _call("ack_material", operation_id)


@activity.defn(name="material_cancel")
async def cancel_material(operation_id: str) -> dict:
    return await _call("cancel_material", operation_id)


@activity.defn(name="material_fail")
async def fail_material(data: dict) -> dict:
    return await _call("fail_material", data["operation_id"], data["code"])


MATERIAL_ACTIVITIES = [prepare_material, submit_material, poll_material, collect_material,
                       ack_material, cancel_material, fail_material]
