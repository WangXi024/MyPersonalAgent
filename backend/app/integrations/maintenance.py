"""Prune expired transport/auth records; business resources and messages are retained."""
from datetime import timedelta

from sqlalchemy import delete

from app.db.models import BusinessEvent, LoginAttempt, Session, now
from app.db.session import SessionFactory


async def prune_expired_records() -> dict:
    cutoff = now()
    async with SessionFactory() as db, db.begin():
        events = await db.execute(delete(BusinessEvent).where(BusinessEvent.created_at < cutoff - timedelta(days=30)))
        sessions = await db.execute(delete(Session).where(Session.expires_at < cutoff))
        attempts = await db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff - timedelta(days=1)))
        return {"events": events.rowcount, "sessions": sessions.rowcount, "login_attempts": attempts.rowcount}
