"""Snapshot model pricing and track which attempts returned token usage."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_model_usage"
down_revision = "0004_model_config"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("operations", sa.Column("usage_details",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True))


def downgrade():
    raise RuntimeError("Destructive downgrade is unsupported; restore a matching encrypted backup.")
