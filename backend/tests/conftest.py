import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, EventStream, User
from app.domain import commands, execution
from app.api import auth, routes
from app import main


@pytest.fixture
async def database(tmp_path, monkeypatch):
    test_url = os.environ.get("APP_TEST_DATABASE_URL")
    schema = "test_" + uuid4().hex
    if test_url:
        admin = create_async_engine(test_url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(test_url, connect_args={"server_settings": {"search_path": schema}})
    else:
        admin = None
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (commands, execution, auth, routes, main):
        monkeypatch.setattr(module, "SessionFactory", factory)
    async with factory() as db, db.begin():
        user = User(username="learner", password_hash=auth.password_hasher.hash("test-only-long-password"))
        other = User(username="other", password_hash="unused")
        db.add_all([user, other])
        await db.flush()
        db.add_all([EventStream(user_id=user.id), EventStream(user_id=other.id)])
    yield factory, user, other
    await engine.dispose()
    if admin:
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()
