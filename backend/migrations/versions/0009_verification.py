"""Immutable confirmed sample tests and separately scoped execution evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_verification"
down_revision = "0008_voice"
branch_labels = None
depends_on = None


def upgrade():
    j = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    def identity():
        return [sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]
    def fk(name, table, optional=False):
        return sa.Column(name, sa.String(36), sa.ForeignKey(table + ".id"), nullable=optional)
    op.create_table("test_contracts", *identity(), fk("user_id", "users"), fk("goal_id", "goals"), fk("task_id", "tasks"),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("requirements_version", sa.Integer(), nullable=False),
        sa.Column("requirements_hash", sa.String(64), nullable=False), sa.Column("contract_hash", sa.String(64), nullable=False),
        sa.Column("comparator", sa.String(30), nullable=False), sa.Column("cases", j, nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False), sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key"), sa.UniqueConstraint("task_id", "version"))
    op.create_table("verification_runs", *identity(), fk("user_id", "users"), fk("goal_id", "goals"), fk("task_id", "tasks"),
        fk("submission_id", "submissions"), fk("attachment_id", "attachments", True), fk("operation_id", "operations"),
        fk("contract_id", "test_contracts"), fk("plan_id", "plans"),
        sa.Column("contract_hash", sa.String(64), nullable=False), sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source", sa.LargeBinary(), nullable=False),
        *[sa.Column(name, sa.Integer(), nullable=False) for name in ("goal_revision", "task_state_version", "requirements_version")],
        sa.Column("status", sa.String(30), nullable=False), sa.Column("stage", sa.String(30), nullable=False),
        *[sa.Column(name, sa.Boolean(), nullable=False) for name in ("tests_verified", "cancel_requested", "cleanup_done")],
        sa.Column("error_message", sa.String(500)), sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("operation_id"))
    op.create_table("verification_cases", *identity(), fk("user_id", "users"), fk("run_id", "verification_runs"),
        sa.Column("index", sa.Integer(), nullable=False), sa.Column("name", sa.String(100), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False, unique=True), sa.Column("command_id", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("reason", sa.String(60)),
        *[sa.Column(name, sa.Boolean(), nullable=False) for name in ("open_attempted", "workspace_created", "seed_saved", "command_attempted",
            "result_saved", "cleanup_done", "output_complete", "output_truncated", "timed_out")],
        sa.Column("exit_code", sa.Integer()), sa.Column("stdout", sa.LargeBinary(), nullable=False),
        sa.Column("stderr", sa.LargeBinary(), nullable=False), sa.Column("utf8_valid", sa.Boolean()), sa.Column("matched", sa.Boolean()),
        sa.Column("finished_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("run_id", "index"))
    for table, names in [("test_contracts", ("user_id", "goal_id", "task_id")),
        ("verification_runs", ("user_id", "goal_id", "task_id", "submission_id")), ("verification_cases", ("user_id", "run_id"))]:
        for name in names:
            op.create_index(f"ix_{table}_{name}", table, [name])


def downgrade():
    raise RuntimeError("Destructive downgrade is unsupported; restore a matching encrypted backup.")
