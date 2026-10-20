"""Allow one general assistant conversation per user in addition to goal conversations."""

from alembic import op
import sqlalchemy as sa

revision = "0002_general_chat"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column("goal_id", existing_type=sa.String(36), nullable=True)
    op.create_index(
        "one_general_conversation",
        "conversations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("goal_id IS NULL"),
        sqlite_where=sa.text("goal_id IS NULL"),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is unsupported; restore a matching encrypted backup.")
