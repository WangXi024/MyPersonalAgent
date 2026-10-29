"""Offline voice configuration, encrypted keys, scoped sessions, transcripts, and independent quota."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_voice"
down_revision = "0007_coding_runs"
branch_labels = None
depends_on = None


def upgrade():
    j = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    def identity():
        return [sa.Column("id", sa.String(36), primary_key=True),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)]
    op.create_table("voice_configs", *identity(),
        sa.Column("provider", sa.String(30)), sa.Column("model_id", sa.String(200)),
        sa.Column("voice", sa.String(100)), sa.Column("endpoint", sa.String(500)),
        sa.Column("api_version", sa.String(80)), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("credential_ciphertext", sa.LargeBinary()), sa.Column("credential_nonce", sa.LargeBinary()),
        sa.Column("credential_key_id", sa.String(64)), sa.Column("last_request_key", sa.String(160), nullable=False),
        sa.Column("last_request_hash", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("voice_sessions", *identity(),
        *[sa.Column(name, sa.String(36), sa.ForeignKey(target), nullable=optional) for name, target, optional in [
            ("user_id", "users.id", False), ("auth_session_id", "sessions.id", False),
            ("conversation_id", "conversations.id", False), ("goal_id", "goals.id", True),
            ("config_id", "voice_configs.id", False)]],
        sa.Column("config_version", sa.Integer(), nullable=False), sa.Column("config_snapshot", j, nullable=False),
        sa.Column("goal_revision", sa.Integer()), sa.Column("memory_revision", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False), sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(60)), sa.Column("error_message", sa.String(500)),
        sa.Column("owner_id", sa.String(100)), sa.Column("ticket_hash", sa.String(64)),
        *[sa.Column(name, sa.DateTime(timezone=True), nullable=name not in {"pending_expires_at", "updated_at"})
          for name in ("ticket_expires_at", "ticket_consumed_at", "lease_until", "pending_expires_at", "started_at",
                       "last_activity_at", "deadline_at", "closed_at", "updated_at")],
        *[sa.Column(name, sa.Integer(), nullable=False) for name in ("max_seconds", "reserved_seconds", "consumed_seconds")],
        *[sa.Column(name, sa.Boolean(), nullable=False) for name in ("quota_settled", "stop_requested", "cleanup_done")],
        sa.Column("usage", j, nullable=False), sa.UniqueConstraint("user_id", "idempotency_key"))
    for name in ("user_id", "conversation_id", "goal_id"):
        op.create_index(f"ix_voice_sessions_{name}", "voice_sessions", [name])
    op.create_table("voice_turns", *identity(),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("voice_sessions.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False), sa.Column("speaker", sa.String(20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False), sa.Column("final", sa.Boolean(), nullable=False),
        sa.Column("interrupted", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "index", "speaker"))
    for name in ("user_id", "session_id"):
        op.create_index(f"ix_voice_turns_{name}", "voice_turns", [name])
    op.create_table("voice_daily_budgets",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("local_date", sa.Date(), primary_key=True),
        sa.Column("reserved_seconds", sa.Integer(), nullable=False),
        sa.Column("consumed_seconds", sa.Integer(), nullable=False))


def downgrade():
    raise RuntimeError("Destructive downgrade is unsupported; restore a matching encrypted backup.")
