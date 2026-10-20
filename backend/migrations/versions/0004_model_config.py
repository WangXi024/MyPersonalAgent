"""Store editable model metadata; credentials remain encrypted in LiteLLM."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_model_config"
down_revision = "0003_conversation_model"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_configs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("api_base", sa.String(500), nullable=False),
        sa.Column("input_cny_per_million", sa.String(30), nullable=False),
        sa.Column("output_cny_per_million", sa.String(30), nullable=False),
        sa.Column("usd_cny_rate", sa.String(30), nullable=False),
        sa.Column("gateway_alias", sa.String(100), nullable=False),
        sa.Column("credential_name", sa.String(100), nullable=False),
        sa.Column("aliases", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_request_key", sa.String(160), nullable=False),
        sa.Column("last_request_hash", sa.String(64), nullable=False),
        sa.Column("last_test_key", sa.String(160), nullable=True),
        sa.Column("test_result", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is unsupported; restore a matching encrypted backup.")
