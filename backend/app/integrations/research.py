"""Bounded public-web clients. No conversation, credentials, or cookies are forwarded."""

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.db.models import now
from app.integrations.settings import runtime_settings


class ResearchError(ValueError):
    """A safe, user-facing failure with no upstream response or credentials attached."""


def public_url(value: str, allowed_domains: str = "") -> str:
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ResearchError("网址格式无效")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        raise ResearchError("网址格式无效") from None
    if (parsed.scheme not in {"https", "http"} or not host or parsed.username is not None
            or parsed.password is not None or port not in {None, 80 if parsed.scheme == "http" else 443}):
        raise ResearchError("仅支持不含登录信息的公开 HTTP/HTTPS 网页")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
        raise ResearchError("不能读取本机或内部网络网址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or any(not label for label in host.split(".")):
            raise ResearchError("不能读取本机或内部网络网址") from None
    else:
        if not address.is_global or address.is_multicast:
            raise ResearchError("不能读取本机或内部网络网址")
    domains = [item.strip().rstrip(".").lower().encode("idna").decode("ascii")
               for item in allowed_domains.split(",") if item.strip()]
    if domains and not any(host == domain or host.endswith("." + domain) for domain in domains):
        raise ResearchError("网址不在配置的资料域名范围内")
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.hidden = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden += 1
        elif tag == "title":
            self.in_title = True
        elif tag in {"p", "div", "li", "br", "h1", "h2", "h3", "tr", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "tr", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden:
            return
        if self.in_title:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


def plain_text(value: str, limit: int) -> str:
    parser = _PageText()
    parser.feed(str(value)[:262144])
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


def source_record(url: str, title: str, snippet: str, provider: str) -> dict:
    return {"id": "src_" + hashlib.sha256(url.encode()).hexdigest()[:20],
            "title": plain_text(title, 200) or urlsplit(url).hostname or "网页资料",
            "url": url, "snippet": plain_text(snippet, 700),
            "provider": provider, "retrieved_at": now().isoformat()}


def _duckduckgo(query: str, maximum: int, timeout: float) -> list[dict]:
    from ddgs import DDGS
    from ddgs.engines import ENGINES

    # DDGS defaults to a multi-engine search. Pin the configured provider explicitly.
    # Its API silently switches to 'auto' if a requested engine is unavailable.
    if "duckduckgo" not in ENGINES.get("text", {}):
        raise ResearchError("配置的搜索服务暂时不可用")
    logging.getLogger("ddgs").setLevel(logging.WARNING)
    return list(DDGS(timeout=int(timeout)).text(query, max_results=maximum,
                                              safesearch="moderate", backend="duckduckgo"))


async def search_web(query: str) -> list[dict]:
    settings = runtime_settings()
    try:
        async with asyncio.timeout(settings.research_timeout_seconds):
            results = await asyncio.to_thread(_duckduckgo, query, settings.research_max_results,
                                             settings.research_timeout_seconds)
    except Exception:
        # Provider exceptions can contain queries, transport details or response bodies.
        raise ResearchError("搜索服务暂时不可用，请稍后再试") from None
    sources: dict[str, dict] = {}
    for result in results[:settings.research_max_results]:
        if not isinstance(result, dict):
            continue
        try:
            url = public_url(str(result.get("href", "")), settings.research_allowed_domains)
        except ResearchError:
            continue
        source = source_record(url, str(result.get("title", "")), str(result.get("body", "")),
                               "duckduckgo")
        sources[source["id"]] = source
    return list(sources.values())


async def _resolve_public(host: str, port: int) -> str:
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ResearchError("无法连接该网页") from None
    if not addresses:
        raise ResearchError("无法连接该网页")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global or address.is_multicast:
            raise ResearchError("不能读取本机或内部网络网址")
    return addresses[0][4][0]


async def _download(url: str, *, transport=None) -> tuple[str, bytes, str]:
    settings = runtime_settings()
    current = public_url(url, settings.research_allowed_domains)
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.research_timeout_seconds, connect=5),
                                 follow_redirects=False, trust_env=False, transport=transport) as client:
        for _ in range(4):
            parsed = urlsplit(current)
            host = parsed.hostname
            ip = await _resolve_public(host, 443 if parsed.scheme == "https" else 80)
            # Pin the validated address, preserving HTTP Host and TLS certificate verification/SNI.
            # A second DNS resolution during connection would permit DNS-rebinding SSRF.
            authority = f"[{ip}]" if ":" in ip else ip
            target = urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, ""))
            client.cookies.clear()
            async with client.stream("GET", target, headers={"Host": parsed.netloc,
                                     "User-Agent": "LearningAgent/1.0 (public learning resources)",
                                     "Accept": "text/html, text/plain, text/markdown, application/json",
                                     "Accept-Encoding": "identity"},
                                     extensions={"sni_hostname": host}) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise ResearchError("网页重定向无效")
                    current = public_url(urljoin(current, location), settings.research_allowed_domains)
                    continue
                response.raise_for_status()
                media = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if media not in {"text/html", "application/xhtml+xml", "text/plain", "text/markdown",
                                 "text/x-markdown", "application/json"}:
                    raise ResearchError("目前仅支持读取文本网页，请上传文档以处理其他文件")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ResearchError("网页使用了暂不支持的压缩格式")
                length = response.headers.get("content-length")
                if length and (not length.isdigit() or int(length) > settings.research_fetch_max_bytes):
                    raise ResearchError("网页超过单次读取大小限制")
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > settings.research_fetch_max_bytes:
                        raise ResearchError("网页超过单次读取大小限制")
                return current, bytes(body), media
    raise ResearchError("网页重定向次数过多")


async def fetch_web(url: str, *, transport=None) -> dict:
    settings = runtime_settings()
    try:
        async with asyncio.timeout(settings.research_timeout_seconds):
            final_url, body, media = await _download(url, transport=transport)
        text = body.decode("utf-8", errors="replace")
        title = ""
        if media in {"text/html", "application/xhtml+xml"}:
            parser = _PageText()
            parser.feed(text)
            title = " ".join(parser.title_parts)
            content = "\n".join(line.strip() for line in " ".join(parser.parts).splitlines() if line.strip())
        else:
            content = text.strip()
        maximum = settings.research_fetch_max_chars
        return {"source": source_record(final_url, title, content[:700], "web"),
                "content": content[:maximum], "truncated": len(content) > maximum}
    except ResearchError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError):
        raise ResearchError("网页暂时无法读取，请检查网址或稍后再试") from None
