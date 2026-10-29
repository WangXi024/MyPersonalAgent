"""Independent Coder runs, trusted command receipts, and exported files."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_coding_runs"
down_revision = "0006_sandbox_execution"
branch_labels = None
depends_on = None


def upgrade():
    j = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    def identity():
        return [sa.Column("id", sa.String(36), primary_key=True),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]
    op.create_table("coding_runs", *identity(),
        *[sa.Column(name, sa.String(36), sa.ForeignKey(target), nullable=optional)
          for name, target, optional in [
              ("user_id", "users.id", False), ("goal_id", "goals.id", False),
              ("task_id", "tasks.id", False), ("submission_id", "submissions.id", False),
              ("operation_id", "operations.id", False), ("attachment_id", "attachments.id", True),
              ("plan_id", "plans.id", False)]],
        *[sa.Column(name, sa.Integer(), nullable=False) for name in ("goal_revision", "requirements_version")],
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("instruction", "source", "summary")],
        sa.Column("workspace_id", sa.String(64), nullable=False, unique=True),
        *[sa.Column(name, sa.Boolean(), nullable=False) for name in
          ("workspace_created", "workspace_open_attempted", "seed_saved", "cleanup_done", "export_saved", "cancel_requested")],
        sa.Column("limits", j, nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("error_message", sa.String(500)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operation_id"))
    op.create_table("coding_commands", *identity(),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("coding_runs.id"), nullable=False),
        sa.Column("remote_id", sa.String(64), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("stdout", sa.Text(), nullable=False),
        sa.Column("stderr", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "remote_id"))
    op.create_table("coding_artifacts", *identity(),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("coding_runs.id"), nullable=False),
        sa.Column("filename", sa.String(1024), nullable=False),
        sa.Column("storage_key", sa.String(160), nullable=False, unique=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("run_id", "filename"))
    for table, names in [("coding_runs", ("user_id", "goal_id", "task_id", "submission_id")),
                         ("coding_commands", ("user_id", "run_id")), ("coding_artifacts", ("user_id", "run_id"))]:
        for name in names:
            op.create_index(f"ix_{table}_{name}", table, [name])


def downgrade():
    raise RuntimeError("Destructive downgrade is unsupported; restore a matching encrypted backup.")
