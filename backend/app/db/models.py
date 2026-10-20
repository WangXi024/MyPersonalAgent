from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


J = JSON().with_variant(JSONB, "postgresql")


class Entity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Entity, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    daily_minutes: Mapped[int] = mapped_column(Integer, default=30)
    availability_version: Mapped[int] = mapped_column(Integer, default=1)
    memory_control_revision: Mapped[int] = mapped_column(Integer, default=1)
    memory_recall_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    proactive_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quiet_start: Mapped[int] = mapped_column(Integer, default=22)
    quiet_end: Mapped[int] = mapped_column(Integer, default=8)
    learning_hour: Mapped[int] = mapped_column(Integer, default=19)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Session(Entity, Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginAttempt(Entity, Base):
    __tablename__ = "login_attempts"
    key_hash: Mapped[str] = mapped_column(String(64), index=True)


class EventStream(Base):
    __tablename__ = "user_event_streams"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)


class Goal(Entity, Base):
    __tablename__ = "goals"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    background: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(Text)
    criteria: Mapped[list] = mapped_column(J, default=list)
    deadline: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    control_revision: Mapped[int] = mapped_column(Integer, default=1)
    active_plan_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("id", "user_id"),)


class Plan(Entity, Base):
    __tablename__ = "plans"
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="proposed")
    summary: Mapped[str] = mapped_column(Text)
    proposal: Mapped[dict] = mapped_column(J)
    availability_version: Mapped[int] = mapped_column(Integer)
    base_plan_id: Mapped[str | None] = mapped_column(String(36))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("goal_id", "version"),
        Index(
            "one_active_plan",
            "goal_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class Milestone(Entity, Base):
    __tablename__ = "milestones"
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    title: Mapped[str] = mapped_column(String(200))
    criteria: Mapped[list] = mapped_column(J)
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="unverified")


class Task(Entity, Base):
    __tablename__ = "tasks"
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    milestone_id: Mapped[str | None] = mapped_column(ForeignKey("milestones.id"))
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text)
    criteria: Mapped[list] = mapped_column(J)
    skills: Mapped[list] = mapped_column(J, default=list)
    requirements_version: Mapped[int] = mapped_column(Integer, default=1)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    active_submission_id: Mapped[str | None] = mapped_column(String(36))
    estimated_minutes: Mapped[int] = mapped_column(Integer)
    reserved_minutes: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(30), default="ready")
    review_of_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))


class Submission(Entity, Base):
    __tablename__ = "submissions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    attachment_ids: Mapped[list] = mapped_column(J, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    requirements_version: Mapped[int] = mapped_column(Integer)
    requirements_snapshot: Mapped[dict] = mapped_column(J)
    study_record_id: Mapped[str | None] = mapped_column(String(36))


class StudyRecord(Entity, Base):
    __tablename__ = "study_records"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    attempt_id: Mapped[str] = mapped_column(String(100))
    local_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(80))
    minutes: Mapped[int] = mapped_column(Integer)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("user_id", "attempt_id"),)


class Evaluation(Entity, Base):
    __tablename__ = "evaluations"
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    feedback: Mapped[str] = mapped_column(Text)
    evidence_type: Mapped[str] = mapped_column(String(30))
    evaluator_version: Mapped[str] = mapped_column(String(100), default="agent-v1")
    rubric_version: Mapped[int] = mapped_column(Integer)
    valid: Mapped[bool] = mapped_column(Boolean)
    details: Mapped[dict] = mapped_column(J)


class SkillProgress(Entity, Base):
    __tablename__ = "skill_progress"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evaluations.id"))
    review_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("goal_id", "name"),)


class Conversation(Entity, Base):
    __tablename__ = "conversations"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id"), unique=True)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    read_seq: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index(
            "one_general_conversation",
            "user_id",
            unique=True,
            postgresql_where=text("goal_id IS NULL"),
            sqlite_where=text("goal_id IS NULL"),
        ),
    )


class Message(Entity, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"))
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    type: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    proactive: Mapped[bool] = mapped_column(Boolean, default=False)
    control_revision: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("conversation_id", "seq"), UniqueConstraint("operation_id", "role"))


class Operation(Entity, Base):
    __tablename__ = "operations"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id"))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="accepted")
    stage: Mapped[str] = mapped_column(String(100), default="queued")
    payload: Mapped[dict] = mapped_column(J, default=dict)
    versions: Mapped[dict] = mapped_column(J, default=dict)
    result: Mapped[dict] = mapped_column(J, default=dict)
    agent_history: Mapped[list] = mapped_column(J, default=list)
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    proactive: Mapped[bool] = mapped_column(Boolean, default=False)
    model_requests: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)


class Outbox(Entity, Base):
    __tablename__ = "outbox"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id"))
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"))
    kind: Mapped[str] = mapped_column(String(40))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(J)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(String(200))


class BusinessEvent(Entity, Base):
    __tablename__ = "business_events"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(36))
    entity_version: Mapped[int] = mapped_column(Integer, default=1)
    goal_id: Mapped[str | None] = mapped_column(String(36))
    operation_id: Mapped[str | None] = mapped_column(String(36))
    summary: Mapped[str] = mapped_column(String(500))
    __table_args__ = (UniqueConstraint("user_id", "seq"),)


class DailyBudget(Base):
    __tablename__ = "daily_budgets"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    authorized_minutes: Mapped[int] = mapped_column(Integer)
    consumed_minutes: Mapped[int] = mapped_column(Integer, default=0)
    reserved_minutes: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Attachment(Entity, Base):
    __tablename__ = "attachments"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    storage_key: Mapped[str] = mapped_column(String(100), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="stored")


class MemorySource(Entity, Base):
    __tablename__ = "memory_sources"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(36))
    source_type: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("user_id", "source_id", "version"),)
