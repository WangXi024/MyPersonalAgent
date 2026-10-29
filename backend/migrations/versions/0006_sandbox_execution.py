"""Store bounded Python execution receipts separately from AI learning reviews."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_sandbox_execution"
down_revision = "0005_model_usage"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table("sandbox_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *[sa.Column(name, sa.String(36), sa.ForeignKey(target), nullable=nullable)
          for name, target, nullable in [
              ("user_id", "users.id", False), ("goal_id", "goals.id", False),
              ("task_id", "tasks.id", False), ("submission_id", "submissions.id", False),
              ("operation_id", "operations.id", False), ("attachment_id", "attachments.id", True),
              ("plan_id", "plans.id", False)]],
        sa.Column("request_id", sa.String(64), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("limits", json_type, nullable=False),
        sa.Column("goal_revision", sa.Integer(), nullable=False),
        sa.Column("requirements_version", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("outcome", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.String(500)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        *[sa.Column(name, sa.Boolean(), nullable=False) for name in
          ("result_saved", "cancel_requested", "cleanup_done", "delivery_done")],
        sa.Column("stdout", sa.Text(), nullable=False),
        sa.Column("stderr", sa.Text(), nullable=False),
        sa.Column("resource_usage", json_type, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operation_id"))
    for name in ("user_id", "goal_id", "task_id", "submission_id"):
        op.create_index(f"ix_sandbox_executions_{name}", "sandbox_executions", [name])
    op.create_table("sandbox_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("execution_id", sa.String(36), sa.ForeignKey("sandbox_executions.id"), nullable=False),
        sa.Column("filename", sa.String(1024), nullable=False),
        sa.Column("storage_key", sa.String(160), nullable=False, unique=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("execution_id", "filename"))
    for name in ("user_id", "execution_id"):
        op.create_index(f"ix_sandbox_artifacts_{name}", "sandbox_artifacts", [name])


def downgrade():
    raise RuntimeError("Destructive downgrade is unsupported; restore a matching encrypted backup.")
