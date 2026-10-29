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
    LargeBinary,
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


class ModelConfig(Entity, Base):
    __tablename__ = "model_configs"
    name: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(20))
    model_id: Mapped[str] = mapped_column(String(200))
    api_base: Mapped[str] = mapped_column(String(500))
    input_cny_per_million: Mapped[str] = mapped_column(String(30))
    output_cny_per_million: Mapped[str] = mapped_column(String(30))
    usd_cny_rate: Mapped[str] = mapped_column(String(30))
    gateway_alias: Mapped[str] = mapped_column(String(100))
    credential_name: Mapped[str] = mapped_column(String(100))
    aliases: Mapped[list] = mapped_column(J, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_request_key: Mapped[str] = mapped_column(String(160))
    last_request_hash: Mapped[str] = mapped_column(String(64))
    last_test_key: Mapped[str | None] = mapped_column(String(160))
    test_result: Mapped[dict | None] = mapped_column(J)


class VoiceConfig(Entity, Base):
    __tablename__ = "voice_configs"
    provider: Mapped[str | None] = mapped_column(String(30))
    model_id: Mapped[str | None] = mapped_column(String(200))
    voice: Mapped[str | None] = mapped_column(String(100))
    endpoint: Mapped[str | None] = mapped_column(String(500))
    api_version: Mapped[str | None] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    credential_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    credential_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    credential_key_id: Mapped[str | None] = mapped_column(String(64))
    last_request_key: Mapped[str] = mapped_column(String(160))
    last_request_hash: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VoiceSession(Entity, Base):
    __tablename__ = "voice_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    auth_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    goal_id: Mapped[str | None] = mapped_column(ForeignKey("goals.id"), index=True)
    config_id: Mapped[str] = mapped_column(ForeignKey("voice_configs.id"))
    config_version: Mapped[int] = mapped_column(Integer)
    config_snapshot: Mapped[dict] = mapped_column(J)
    goal_revision: Mapped[int | None] = mapped_column(Integer)
    memory_revision: Mapped[int] = mapped_column(Integer)
    local_date: Mapped[date] = mapped_column(Date)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    reason: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(String(500))
    owner_id: Mapped[str | None] = mapped_column(String(100))
    ticket_hash: Mapped[str | None] = mapped_column(String(64))
    ticket_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ticket_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_seconds: Mapped[int] = mapped_column(Integer)
    reserved_seconds: Mapped[int] = mapped_column(Integer)
    consumed_seconds: Mapped[int] = mapped_column(Integer, default=0)
    quota_settled: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_done: Mapped[bool] = mapped_column(Boolean, default=False)
    usage: Mapped[dict] = mapped_column(J, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)


class VoiceTurn(Entity, Base):
    __tablename__ = "voice_turns"
    session_id: Mapped[str] = mapped_column(ForeignKey("voice_sessions.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("session_id", "index", "speaker"),)


class VoiceDailyBudget(Base):
    __tablename__ = "voice_daily_budgets"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_seconds: Mapped[int] = mapped_column(Integer, default=0)
    consumed_seconds: Mapped[int] = mapped_column(Integer, default=0)


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


class SandboxExecution(Entity, Base):
    __tablename__ = "sandbox_executions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    attachment_id: Mapped[str | None] = mapped_column(ForeignKey("attachments.id"))
    request_id: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    code: Mapped[str] = mapped_column(Text)
    limits: Mapped[dict] = mapped_column(J)
    goal_revision: Mapped[int] = mapped_column(Integer)
    requirements_version: Mapped[int] = mapped_column(Integer)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    language: Mapped[str] = mapped_column(String(20), default="python")
    status: Mapped[str] = mapped_column(String(30), default="accepted")
    phase: Mapped[str] = mapped_column(String(30), default="queued")
    outcome: Mapped[str] = mapped_column(String(30), default="")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(500))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_done: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_done: Mapped[bool] = mapped_column(Boolean, default=False)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    resource_usage: Mapped[dict] = mapped_column(J, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SandboxArtifact(Entity, Base):
    __tablename__ = "sandbox_artifacts"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("sandbox_executions.id"), index=True)
    filename: Mapped[str] = mapped_column(String(1024))
    storage_key: Mapped[str] = mapped_column(String(160), unique=True)
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("execution_id", "filename"),)


class CodingRun(Entity, Base):
    __tablename__ = "coding_runs"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), index=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    attachment_id: Mapped[str | None] = mapped_column(ForeignKey("attachments.id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    goal_revision: Mapped[int] = mapped_column(Integer)
    requirements_version: Mapped[int] = mapped_column(Integer)
    instruction: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[str] = mapped_column(String(64), unique=True)
    workspace_created: Mapped[bool] = mapped_column(Boolean, default=False)
    workspace_open_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_done: Mapped[bool] = mapped_column(Boolean, default=False)
    export_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    limits: Mapped[dict] = mapped_column(J, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="accepted")
    stage: Mapped[str] = mapped_column(String(30), default="queued")
    summary: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CodingCommand(Entity, Base):
    __tablename__ = "coding_commands"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id"), index=True)
    remote_id: Mapped[str] = mapped_column(String(64))
    command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="running")
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("run_id", "remote_id"),)


class CodingArtifact(Entity, Base):
    __tablename__ = "coding_artifacts"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("coding_runs.id"), index=True)
    filename: Mapped[str] = mapped_column(String(1024))
    storage_key: Mapped[str] = mapped_column(String(160), unique=True)
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("run_id", "filename"),)


class TestContract(Entity, Base):
    __tablename__ = "test_contracts"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    requirements_version: Mapped[int] = mapped_column(Integer)
    requirements_hash: Mapped[str] = mapped_column(String(64))
    contract_hash: Mapped[str] = mapped_column(String(64))
    comparator: Mapped[str] = mapped_column(String(30))
    cases: Mapped[list] = mapped_column(J)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"), UniqueConstraint("task_id", "version"))


class VerificationRun(Entity, Base):
    __tablename__ = "verification_runs"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    goal_id: Mapped[str] = mapped_column(ForeignKey("goals.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), index=True)
    attachment_id: Mapped[str | None] = mapped_column(ForeignKey("attachments.id"))
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), unique=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("test_contracts.id"))
    contract_hash: Mapped[str] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[bytes] = mapped_column(LargeBinary)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    goal_revision: Mapped[int] = mapped_column(Integer)
    task_state_version: Mapped[int] = mapped_column(Integer)
    requirements_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="accepted")
    stage: Mapped[str] = mapped_column(String(30), default="queued")
    tests_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_done: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(String(500))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VerificationCase(Entity, Base):
    __tablename__ = "verification_cases"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("verification_runs.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    workspace_id: Mapped[str] = mapped_column(String(64), unique=True)
    command_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    reason: Mapped[str | None] = mapped_column(String(60))
    open_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    workspace_created: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    command_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    result_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_done: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout: Mapped[bytes] = mapped_column(LargeBinary, default=bytes)
    stderr: Mapped[bytes] = mapped_column(LargeBinary, default=bytes)
    output_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    output_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    utf8_valid: Mapped[bool | None] = mapped_column(Boolean)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    matched: Mapped[bool | None] = mapped_column(Boolean)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("run_id", "index"),)


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
    model_alias: Mapped[str | None] = mapped_column(String(100))
    model_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

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
    usage_details: Mapped[dict | None] = mapped_column(J)
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
