"""Persist a model selection for each conversation."""

from alembic import op
import sqlalchemy as sa

revision = "0003_conversation_model"
down_revision = "0002_general_chat"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("model_alias", sa.String(100), nullable=True))
    op.add_column("conversations", sa.Column("model_version", sa.Integer(), server_default="1", nullable=False))


def downgrade():
    raise RuntimeError("Destructive downgrade is unsupported; restore a matching encrypted backup.")
