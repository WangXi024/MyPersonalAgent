import httpx

from app.main import app


async def test_auth_csrf_and_untrusted_origin(database):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        assert (await client.get("/api/v1/bootstrap")).status_code == 401
        credentials = {"username": "learner", "password": "test-only-long-password"}
        assert (
            await client.post(
                "/api/v1/auth/login", json=credentials, headers={"Origin": "https://evil.example"}
            )
        ).status_code == 403
        login = await client.post(
            "/api/v1/auth/login", json=credentials, headers={"Origin": "http://localhost:8080"}
        )
        assert login.status_code == 200
        assert "password_hash" not in login.text
        csrf = login.json()["csrf_token"]
        assert "HttpOnly" in login.headers.get_list("set-cookie")[0]
        payload = {"title": "Python", "outcome": "完成小工具"}
        headers = {"Origin": "http://localhost:8080", "Idempotency-Key": "test-create-0001"}
        assert (await client.post("/api/v1/goals", json=payload, headers=headers)).status_code == 403
        headers["X-CSRF-Token"] = csrf
        result = await client.post("/api/v1/goals", json=payload, headers=headers)
        assert result.status_code == 202, result.text
        snapshot = await client.get("/api/v1/bootstrap")
        assert snapshot.status_code == 200
        assert len(snapshot.json()["goals"]) == 1
        assert snapshot.json()["cursor"] >= 2
        assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 200
        assert (await client.get("/api/v1/bootstrap")).status_code == 401


async def test_preference_change_does_not_cancel_tasks(database):
    from app.api.auth import current_user
    from app.db.models import Task
    from test_domain import active_goal

    factory, user, other = database
    goal, task, _ = await active_goal(user)
    app.dependency_overrides[current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
        ) as client:
            response = await client.patch(
                "/api/v1/settings", json={"expected_version": 1, "proactive_enabled": False}
            )
            assert response.status_code == 200
        async with factory() as db:
            assert (await db.get(Task, task["id"])).status == "ready"
    finally:
        app.dependency_overrides.clear()


async def test_attachment_upload_retry_has_one_record_and_file(database, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy import func, select
    from app.api import routes
    from app.api.auth import current_user
    from app.db.models import Attachment

    factory, user, other = database
    app.dependency_overrides[current_user] = lambda: user
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(max_upload_bytes=1024, attachment_dir=tmp_path / "uploads"),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
        ) as client:
            headers = {"Idempotency-Key": "attachment-retry-test"}
            first = await client.post(
                "/api/v1/attachments",
                files={"file": ("note.txt", b"safe text", "text/plain")},
                headers=headers,
            )
            second = await client.post(
                "/api/v1/attachments",
                files={"file": ("note.txt", b"safe text", "text/plain")},
                headers=headers,
            )
            assert first.status_code == second.status_code == 200
            assert first.json()["id"] == second.json()["id"]
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(Attachment)) == 1
        assert len(list((tmp_path / "uploads").iterdir())) == 1
    finally:
        app.dependency_overrides.clear()


async def test_goal_draft_list_and_confirmation_api(database):
    from app.api.auth import current_user
    from test_domain import pending_goal_draft

    factory, user, other = database
    conversation, source = await pending_goal_draft(user)
    path = f"/api/v1/goal-drafts/{source['id']}/accept"
    listing = f"/api/v1/conversations/{conversation['id']}/goal-drafts"
    payload = source["result"]["goal_draft"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        assert (await client.get(listing)).status_code == 401
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "learner", "password": "test-only-long-password"},
            headers={"Origin": "http://localhost:8080"},
        )
        headers = {"Origin": "http://localhost:8080", "Idempotency-Key": "confirm-goal-draft"}
        assert (await client.post(path, json=payload, headers=headers)).status_code == 403
        headers["X-CSRF-Token"] = login.json()["csrf_token"]
        cards = (await client.get(listing)).json()["items"]
        assert len(cards) == 1 and cards[0]["status"] == "pending"
        invalid = await client.post(path, json={**payload, "criteria": []}, headers=headers)
        assert invalid.status_code == 422
        response = await client.post(path, json={**payload, "title": "确认时修改"}, headers=headers)
        assert response.status_code == 202
        replay = await client.post(path, json={**payload, "title": "确认时修改"}, headers=headers)
        assert replay.status_code == 202 and replay.json() == response.json()
        conflict = await client.post(path, json=payload, headers=headers)
        assert conflict.status_code == 409
        cards = (await client.get(listing)).json()["items"]
        assert cards[0]["status"] == "accepted" and cards[0]["draft"]["title"] == "确认时修改"
        assert cards[0]["goal_id"] == response.json()["resource_id"]
        app.dependency_overrides[current_user] = lambda: other
        try:
            assert (await client.get(listing)).status_code == 404
            assert (await client.post(path, json=payload, headers=headers)).status_code == 404
        finally:
            app.dependency_overrides.clear()
