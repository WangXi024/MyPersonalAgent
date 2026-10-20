"""Controlled account creation. Secrets are read from environment or an interactive prompt, never printed."""

import argparse
import asyncio
import getpass
import os

from sqlalchemy import select

from app.api.auth import password_hasher
from app.config import get_settings
from app.db.models import EventStream, User
from app.db.session import SessionFactory, engine


async def bootstrap(username: str, password: str) -> bool:
    if not username or len(username) > 100:
        raise ValueError("账号名称不能为空且不能超过 100 字符")
    if len(password) < 12 or password in {"<REDACTED>", "changeme", "password123456"}:
        raise ValueError("密码必须至少 12 字符，不能使用示例占位符")
    async with SessionFactory() as db, db.begin():
        existing = await db.scalar(select(User).where(User.username == username))
        if existing:
            return False
        user = User(
            username=username,
            password_hash=password_hasher.hash(password),
            timezone=get_settings().default_timezone,
        )
        db.add(user)
        await db.flush()
        db.add(EventStream(user_id=user.id))
        return True


async def run(args):
    username = os.environ.get("BOOTSTRAP_USERNAME") or input("Username: ").strip()
    password = os.environ.get("BOOTSTRAP_PASSWORD") or getpass.getpass("Password: ")
    try:
        created = await bootstrap(username, password)
        print("Account created." if created else "Account already exists; credentials unchanged.")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["bootstrap", "create-user"])
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
