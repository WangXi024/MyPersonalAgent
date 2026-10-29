"""Bounded quiz attempts; practice feedback never changes learning evidence or time."""

from copy import deepcopy
from uuid import uuid4

from app.db.models import Conversation, Goal, Operation, now
from app.db.session import SessionFactory
from app.domain.common import DomainError, digest, emit, owned
from app.domain.execution import operation_locked


async def submit_attempt(user_id, operation_id, quiz_id, answers, key):
    async with SessionFactory() as db, db.begin():
        await owned(db, Operation, operation_id, user_id)
        op, user, stream, _ = await operation_locked(db, operation_id)
        conversation = await db.get(Conversation, op.payload.get("conversation_id"))
        goal = await db.get(Goal, op.goal_id) if op.goal_id else None
        if (not user.active or op.kind != "tutor" or op.status != "succeeded" or not conversation
                or conversation.user_id != user_id or conversation.goal_id != op.goal_id
                or op.goal_id and (not goal or goal.user_id != user_id)):
            raise DomainError("quiz_unavailable", "本次练习不可用", 409)
        saved = deepcopy(op.result or {})
        quiz = saved.get("private_quizzes", {}).get(quiz_id)
        blocks = saved.get("ui_components", [])
        block = next((item for item in blocks if item.get("type") == "quiz" and item.get("quiz_id") == quiz_id
                      and item.get("operation_id") == operation_id), None)
        if not quiz or not block:
            raise DomainError("not_found", "练习不存在", 404)
        question_ids = [question["id"] for question in quiz["questions"]]
        selected = {item["question_id"]: item["choice_id"] for item in answers}
        if len(answers) != len(selected) or set(selected) != set(question_ids):
            raise DomainError("invalid_quiz_answers", "请为每道题选择一个答案", 422)
        if any(selected[q["id"]] not in {choice["id"] for choice in q["choices"]} for q in quiz["questions"]):
            raise DomainError("invalid_quiz_answers", "所选答案不属于本次题目", 422)
        canonical = [{"question_id": identifier, "choice_id": selected[identifier]} for identifier in question_ids]
        fingerprint = digest({"quiz_id": quiz_id, "answers": canonical})
        requests = saved.setdefault("quiz_request_keys", {})
        if key in requests:
            if requests[key]["hash"] != fingerprint:
                raise DomainError("idempotency_conflict", "该请求标识已用于不同练习答案")
            return requests[key]["attempt"]
        attempts = quiz.setdefault("attempts", [])
        if len(attempts) >= 5:
            raise DomainError("quiz_attempt_limit", "本组练习已达到五次自测上限", 429)
        feedback = [{"question_id": question["id"], "selected_choice_id": selected[question["id"]],
            "correct_choice_id": question["correct_choice_id"],
            "correct": selected[question["id"]] == question["correct_choice_id"],
            "explanation": question["explanation"]} for question in quiz["questions"]]
        attempt = {"id": str(uuid4()), "quiz_id": quiz_id, "attempt_number": len(attempts) + 1,
            "submitted_at": now().isoformat(), "practice_only": True, "answers": canonical,
            "correct_count": sum(item["correct"] for item in feedback), "total": len(question_ids), "feedback": feedback}
        attempts.append(attempt)
        requests[key] = {"hash": fingerprint, "attempt": attempt}
        block["attempt"], block["attempt_count"] = attempt, len(attempts)
        op.result, op.updated_at = saved, now()
        await emit(db, stream, "quiz.attempted", op.id, "练习反馈已保存，不计入学习评估", op.goal_id, op.id)
        return attempt
