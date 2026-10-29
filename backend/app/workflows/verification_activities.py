"""Only small identifiers/status cross Temporal boundaries, never source or result bytes."""

from temporalio import activity
from temporalio.exceptions import ApplicationError


async def _call(name, operation_id, **kwargs):
    from app.domain import verification
    from app.domain.common import DomainError
    failure = None
    try:
        return await getattr(verification, name)(operation_id, **kwargs)
    except DomainError as error:
        failure = ApplicationError(error.message, type=error.code, non_retryable=True)
    except Exception:
        failure = ApplicationError("样例验证活动暂不可用", type="VerificationActivityError")
    raise failure


@activity.defn(name="verification_prepare")
async def prepare(operation_id: str) -> dict:
    return await _call("prepare_verification", operation_id)


@activity.defn(name="verification_advance")
async def advance(operation_id: str) -> dict:
    return await _call("advance_verification", operation_id)


@activity.defn(name="verification_stop")
async def stop(data: dict) -> dict:
    return await _call("stop_verification", data["operation_id"], expired=data.get("expired") is True)


@activity.defn(name="verification_fail")
async def fail(operation_id: str) -> dict:
    return await _call("fail_verification", operation_id)


VERIFICATION_ACTIVITIES = [prepare, advance, stop, fail]
