"""Apply explicit chat commands through the same locked domain operations as the UI."""
from pydantic import ValidationError
from sqlalchemy import select

from app.agents.schemas import TutorProposal
from app.db.models import Goal, Operation
from app.domain import commands
from app.domain.common import DomainError, dispatch, new_operation, owned, version


async def apply_chat_actions(db, user, stream, source, result):
    try:
        actions = TutorProposal.model_validate({k: v for k, v in result.items() if k != "agent_history"}).actions
    except ValidationError:
        raise DomainError("invalid_chat_actions", "对话操作格式无效，未执行修改", 422) from None
    receipts = []
    for index, action in enumerate(actions):
        receipt = {"kind": action.kind, "goal_id": None, "goal_title": "", "operation_id": None}
        try:
            async with db.begin_nested():
                quote = action.request_quote.strip()
                if quote not in str(source.payload.get("content", "")):
                    raise DomainError("missing_user_request", "没有找到本条消息中的操作要求，未执行修改")
                if source.goal_id and source.goal_id != action.goal_id:
                    raise DomainError("wrong_goal", "请在个人对话中操作其他目标")
                goal = await owned(db, Goal, action.goal_id, user.id)
                receipt.update(goal_id=goal.id, goal_title=goal.title)
                version(goal.control_revision, action.expected_revision)
                key = f"chat-action:{source.id}:{index}"
                if action.kind == "cancel_goal":
                    await commands.control_goal_in_transaction(
                        db, user, stream, goal, "cancel", action.expected_revision, key, source.id)
                    notice = "目标已取消，历史记录保留。"
                elif action.kind == "update_goal":
                    changes = action.changes.model_dump(mode="json", exclude_none=True)
                    response = await commands.edit_goal_in_transaction(
                        db, user, stream, goal, changes, action.expected_revision, key, source.id,
                        instruction=action.instruction or "根据用户刚确认的目标要求重新规划。")
                    receipt["operation_id"] = response["operation_id"]
                    receipt["changes"] = changes
                    notice = "目标要求已更新，正在准备新计划；生成后请确认。"
                else:
                    if goal.status not in {"draft", "active"}:
                        raise DomainError("goal_inactive", "请先恢复目标后再生成计划")
                    previous = None
                    if action.kind == "retry_plan":
                        previous = await owned(db, Operation, action.operation_id, user.id)
                        if (previous.goal_id != goal.id or previous.kind != "plan"
                                or previous.status not in {"failed", "superseded"}):
                            raise DomainError("invalid_retry", "这条记录不是该目标可重试的规划操作")
                    latest = await db.scalar(select(Operation).where(
                        Operation.goal_id == goal.id, Operation.kind == "plan"
                    ).order_by(Operation.created_at.desc()).limit(1))
                    if latest and (latest.status in {"accepted", "running"} or
                                   (previous and latest.created_at > previous.created_at
                                    and latest.status == "succeeded")):
                        receipt["operation_id"] = latest.id
                        notice = "已有计划正在处理或等待确认，请查看下方进度。"
                    else:
                        payload = {**(previous.payload if previous else {}),
                                   "instruction": action.instruction or "根据最新目标和用户要求准备学习计划。",
                                   "source_operation_id": source.id}
                        if previous:
                            payload["retry_of_operation_id"] = previous.id
                        follow, created = await new_operation(db, user, stream, key, "plan", payload, goal)
                        if created:
                            await dispatch(db, follow, "goal_created")
                        receipt["operation_id"] = follow.id
                        notice = "已开始准备学习计划，生成后请确认。"
                receipt.update(status="applied", message=notice)
        except DomainError as error:
            # A rejected command must not turn the model's prose into a false success receipt.
            await db.refresh(stream)
            receipt.update(status="blocked", message=error.message, error_code=error.code)
        receipts.append(receipt)
    return receipts
