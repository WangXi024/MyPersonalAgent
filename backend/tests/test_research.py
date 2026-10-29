from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from app.api import routes
from app.db.models import Operation, now
from app.domain import commands, research
from app.integrations import research as web
from app.integrations.settings import RuntimeSettings


@pytest.fixture
def settings(monkeypatch):
    config = RuntimeSettings(research_enabled=True, research_max_calls=2, research_fetch_max_chars=1000,
                             research_fetch_max_bytes=16384)
    monkeypatch.setattr(web, "runtime_settings", lambda: config)
    monkeypatch.setattr(research, "runtime_settings", lambda: config)
    return config


async def say(database, monkeypatch, *, enabled=True):
    factory, user, other = database
    monkeypatch.setattr(research, "SessionFactory", factory)
    conversation = await routes.assistant_conversation(user)
    response = await commands.send_message(user.id, conversation["id"],
                                           {"content": "查找 Python 官方文档", "research_requested": enabled},
                                           str(uuid4()))
    # Keep the tool boundary test independent of the command/API's own opt-in tests.
    async with factory() as db, db.begin():
        op = await db.get(Operation, response["operation_id"])
        op.payload = {**op.payload, "research_requested": enabled}
    return response["operation_id"]


def source():
    return web.source_record("https://docs.python.org/3/tutorial/", "Python Tutorial", "Learn Python", "duckduckgo")


async def test_search_receipt_persists_and_replay_does_not_search_again(database, monkeypatch, settings):
    operation = await say(database, monkeypatch)
    queries = []

    async def search(query):
        queries.append(query)
        return [source()]

    monkeypatch.setattr(web, "search_web", search)
    first = await research.search_for_operation(operation, "Python  official\n tutorial", "call-1")
    repeated = await research.search_for_operation(operation, "Python official tutorial", "call-1")
    assert first == repeated and first["status"] == "ok"
    assert queries == ["Python official tutorial"]
    factory, _, _ = database
    async with factory() as db:
        op = await db.get(Operation, operation)
        assert op.result["research_sources"] == first["sources"]
        assert len(op.result["research_calls"]) == 1
        assert "query" not in op.result["research_calls"][0]
    changed = await research.search_for_operation(operation, "different query", "call-1")
    assert changed["status"] == "invalid_request" and len(queries) == 1


@pytest.mark.parametrize("state,expected", [("no_opt_in", "permission_denied"),
                                            ("cancelled", "operation_terminal"),
                                            ("old", "operation_deadline"),
                                            ("wrong_conversation", "permission_denied"),
                                            ("wrong_kind", "permission_denied")])
async def test_scope_denies_external_io(database, monkeypatch, settings, state, expected):
    operation = await say(database, monkeypatch, enabled=state != "no_opt_in")
    factory, _, other = database
    wrong_conversation = await routes.assistant_conversation(other) if state == "wrong_conversation" else None
    async with factory() as db, db.begin():
        op = await db.get(Operation, operation)
        if state == "cancelled":
            op.status = "cancelled"
        elif state == "old":
            op.created_at = now() - timedelta(minutes=31)
        elif state == "wrong_kind":
            op.kind = "plan"
        elif state == "wrong_conversation":
            op.payload = {**op.payload, "conversation_id": wrong_conversation["id"]}

    async def never(_):
        pytest.fail("unauthorized external call")

    monkeypatch.setattr(web, "search_web", never)
    response = await research.search_for_operation(operation, "Python", "call-1")
    assert response["status"] == expected
    async with factory() as db:
        assert not (await db.get(Operation, operation)).result.get("research_calls")


async def test_disabled_and_invalid_requests_do_not_consume_calls(database, monkeypatch, settings):
    operation = await say(database, monkeypatch)
    settings.research_enabled = False
    assert (await research.search_for_operation(operation, "Python", "call-1"))["status"] == "disabled"
    settings.research_enabled = True
    assert (await research.search_for_operation(operation, "x" * 301, "call-2"))["status"] == "invalid_request"
    assert (await research.fetch_for_operation(operation, "http://127.0.0.1", "call-3"))["status"] == "invalid_request"
    async with database[0]() as db:
        assert not (await db.get(Operation, operation)).result.get("research_calls")


async def test_failed_searches_share_the_persisted_call_budget(database, monkeypatch, settings):
    operation = await say(database, monkeypatch)
    requests = []

    async def unavailable(query):
        requests.append(query)
        raise web.ResearchError("搜索服务暂时不可用")

    monkeypatch.setattr(web, "search_web", unavailable)
    first = await research.search_for_operation(operation, "Python", "call-1")
    assert first["status"] == "unavailable"
    assert await research.search_for_operation(operation, "Python", "call-1") == first
    assert (await research.search_for_operation(operation, "Python", "call-2"))["status"] == "unavailable"
    assert (await research.search_for_operation(operation, "Python", "call-3"))["status"] == "budget_exceeded"
    assert requests == ["Python", "Python"]


async def test_cancel_during_io_does_not_publish_late_sources(database, monkeypatch, settings):
    operation = await say(database, monkeypatch)
    factory = database[0]

    async def slow_search(_):
        async with factory() as db, db.begin():
            (await db.get(Operation, operation)).status = "cancelled"
        return [source()]

    monkeypatch.setattr(web, "search_web", slow_search)
    assert (await research.search_for_operation(operation, "Python", "call-1"))["status"] == "operation_terminal"
    async with factory() as db:
        assert not (await db.get(Operation, operation)).result.get("research_sources")


async def test_uncertain_retry_does_not_repeat_external_request(database, monkeypatch, settings):
    operation = await say(database, monkeypatch)
    assert (await research._reserve(operation, "search", "Python", "call-1"))[0] is True

    async def never(_):
        pytest.fail("An interrupted call must not send another external request")

    monkeypatch.setattr(web, "search_web", never)
    assert (await research.search_for_operation(operation, "Python", "call-1"))["status"] == "unavailable"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "http://localhost/",
                                  "http://localhost./", "http://host.internal/", "http://192.168.1.2/",
                                  "http://127.0.0.1/", "http://169.254.169.254/", "http://[::1]/",
                                  "http://[::ffff:127.0.0.1]/", "https://example.com:8443/",
                                  "https://user:password@example.com/", "https://example.com/\nheader",
                                  "https://example.com\\@localhost/"])
def test_private_and_unsupported_urls_are_rejected(url):
    with pytest.raises(web.ResearchError):
        web.public_url(url)


def test_allowlist_matches_domain_boundaries_and_canonicalizes_urls():
    assert web.public_url("https://Docs.Python.org:443/3/#tutorial", "python.org") == "https://docs.python.org/3/"
    with pytest.raises(web.ResearchError):
        web.public_url("https://python.org.evil.example/", "python.org")


async def test_search_filters_sources_and_scrubs_provider_exceptions(monkeypatch, settings):
    def results(query, maximum, timeout):
        assert query == "Python"
        return [{"href": "http://127.0.0.1/", "title": "blocked"},
                {"href": "https://docs.python.org/3/", "title": "<b>Python</b>", "body": "<script>bad</script>good"}]

    monkeypatch.setattr(web, "_duckduckgo", results)
    response = await web.search_web("Python")
    assert len(response) == 1 and response[0]["title"] == "Python" and response[0]["snippet"] == "good"

    def failing(*_):
        raise RuntimeError("provider credential=secret, private query")

    monkeypatch.setattr(web, "_duckduckgo", failing)
    with pytest.raises(web.ResearchError) as raised:
        await web.search_web("Python")
    assert "secret" not in str(raised.value) and "private query" not in str(raised.value)


async def test_dns_rebinding_cannot_change_the_connected_address(monkeypatch, settings):
    resolved = []

    async def public(host, port):
        resolved.append((host, port))
        return "93.184.216.34"

    def page(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "docs.example.com"
        assert request.extensions["sni_hostname"] == "docs.example.com"
        return httpx.Response(200, headers={"content-type": "text/html"},
                              stream=httpx.ByteStream(b"<title>Guide</title><h1>Learn</h1><script>hidden</script><p>Python</p>"))

    monkeypatch.setattr(web, "_resolve_public", public)
    result = await web.fetch_web("https://docs.example.com/guide", transport=httpx.MockTransport(page))
    assert result["source"]["url"] == "https://docs.example.com/guide"
    assert result["source"]["title"] == "Guide"
    assert "Learn" in result["content"] and "hidden" not in result["content"]
    assert resolved == [("docs.example.com", 443)]


async def test_redirect_to_private_network_is_blocked(monkeypatch, settings):
    requests = []

    async def public(host, port):
        return "93.184.216.34"

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    monkeypatch.setattr(web, "_resolve_public", public)
    with pytest.raises(web.ResearchError, match="内部网络"):
        await web.fetch_web("https://docs.example.com/", transport=httpx.MockTransport(redirect))
    assert len(requests) == 1


async def test_mixed_public_private_dns_answer_is_rejected(monkeypatch):
    def addresses(*args, **kwargs):
        return [(web.socket.AF_INET, web.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (web.socket.AF_INET, web.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(web.socket, "getaddrinfo", addresses)
    with pytest.raises(web.ResearchError, match="内部网络"):
        await web._resolve_public("rebind.example.com", 443)


@pytest.mark.parametrize("headers,body,expected", [
    ({"content-type": "text/plain", "content-length": "9999999"}, b"small", "大小限制"),
    ({"content-type": "text/plain"}, b"x" * 16385, "大小限制"),
    ({"content-type": "application/pdf"}, b"binary", "文本网页"),
    ({"content-type": "text/html", "content-encoding": "gzip"}, b"ignored", "压缩格式"),
])
async def test_download_limits_are_enforced(monkeypatch, settings, headers, body, expected):
    async def public(host, port):
        return "93.184.216.34"

    monkeypatch.setattr(web, "_resolve_public", public)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, stream=httpx.ByteStream(body)))
    with pytest.raises(web.ResearchError, match=expected):
        await web.fetch_web("https://docs.example.com/", transport=transport)


async def test_fetch_content_is_bounded_and_source_id_stable(monkeypatch, settings):
    async def public(host, port):
        return "93.184.216.34"

    monkeypatch.setattr(web, "_resolve_public", public)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/plain"},
                                                            stream=httpx.ByteStream(b"x" * 2000)))
    result = await web.fetch_web("https://docs.example.com/", transport=transport)
    assert len(result["content"]) == 1000 and result["truncated"] is True
    assert result["source"]["id"] == web.source_record("https://docs.example.com/", "", "", "web")["id"]
