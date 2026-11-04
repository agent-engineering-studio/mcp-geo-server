"""Web UI access control (Basic auth, rate limit), catalog cache, Phase-0 defaults."""

from __future__ import annotations

import base64

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mcp_geo_server import config
from webui.security import BasicAuthMiddleware, RateLimiter, RateLimitMiddleware


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _app() -> Starlette:
    async def ok(_request):
        return PlainTextResponse("ok")
    return Starlette(routes=[Route(p, ok, methods=["GET", "POST"])
                             for p in ("/", "/healthz", "/api/ask", "/api/layers")])


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


# ---- Basic auth -----------------------------------------------------------
async def test_auth_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("WEBUI_USER", raising=False)
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    mw = BasicAuthMiddleware(_app())
    assert not mw.enabled
    async with _client(mw) as c:
        assert (await c.get("/api/layers")).status_code == 200


async def test_auth_needs_both_env_vars(monkeypatch):
    monkeypatch.setenv("WEBUI_USER", "alice")
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    assert not BasicAuthMiddleware(_app()).enabled


async def test_auth_rejects_missing_and_wrong_credentials():
    mw = BasicAuthMiddleware(_app(), credentials=("alice", "s3cret"))
    async with _client(mw) as c:
        r = await c.get("/api/layers")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic ")
        assert (await c.get("/", headers=_basic("alice", "nope"))).status_code == 401
        assert (await c.get("/", headers=_basic("bob", "s3cret"))).status_code == 401
        assert (await c.get("/", headers={"Authorization": "Basic !!!"})).status_code == 401
        assert (await c.get("/", headers={"Authorization": "Bearer x"})).status_code == 401


async def test_auth_accepts_valid_credentials_and_password_with_colon():
    mw = BasicAuthMiddleware(_app(), credentials=("alice", "a:b:c"))
    async with _client(mw) as c:
        assert (await c.get("/api/layers", headers=_basic("alice", "a:b:c"))).status_code == 200


async def test_healthz_is_exempt_from_auth():
    mw = BasicAuthMiddleware(_app(), credentials=("alice", "s3cret"))
    async with _client(mw) as c:
        assert (await c.get("/healthz")).status_code == 200


# ---- rate limit -----------------------------------------------------------
def test_rate_limiter_sliding_window():
    rl = RateLimiter(limit=2, window=60)
    assert rl.hit("ip", now=0) is None
    assert rl.hit("ip", now=1) is None
    assert rl.hit("ip", now=2) == pytest.approx(58)
    assert rl.hit("other", now=2) is None          # per key
    assert rl.hit("ip", now=60.5) is None          # first hit expired


def test_rate_limiter_zero_disables():
    rl = RateLimiter(limit=0)
    assert all(rl.hit("ip", now=t) is None for t in range(100))


async def test_rate_limit_middleware_only_on_costly_paths():
    mw = RateLimitMiddleware(_app(), limiter=RateLimiter(limit=1))
    async with _client(mw) as c:
        assert (await c.post("/api/ask")).status_code == 200
        r = await c.post("/api/ask")
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
        # Cheap routes are never throttled.
        for _ in range(5):
            assert (await c.get("/api/layers")).status_code == 200


def test_rate_limit_env_default(monkeypatch):
    monkeypatch.delenv("WEBUI_RATE_LIMIT", raising=False)
    assert RateLimitMiddleware(_app()).limiter.limit == 30
    monkeypatch.setenv("WEBUI_RATE_LIMIT", "0")
    assert RateLimitMiddleware(_app()).limiter.limit == 0


# ---- catalog cache --------------------------------------------------------
async def test_catalog_is_cached_and_invalidated(monkeypatch):
    from webui import app as webui_app

    calls = []

    async def fake_fetch():
        calls.append(1)
        return [f"catalog-{len(calls)}"]

    monkeypatch.setattr(webui_app, "_fetch_catalog", fake_fetch)
    monkeypatch.setattr(webui_app, "_CATALOG_TTL", 60.0)
    webui_app._invalidate_catalog()

    assert await webui_app._load_catalog() == ["catalog-1"]
    assert await webui_app._load_catalog() == ["catalog-1"]
    assert len(calls) == 1

    webui_app._invalidate_catalog()
    assert await webui_app._load_catalog() == ["catalog-2"]

    monkeypatch.setattr(webui_app, "_CATALOG_TTL", 0.0)   # 0 = no cache
    await webui_app._load_catalog()
    await webui_app._load_catalog()
    assert len(calls) == 4
    webui_app._invalidate_catalog()


async def test_app_healthz_and_auth_wired(monkeypatch):
    """The real app answers /healthz without GeoServer and enforces auth."""
    from webui import app as webui_app

    async with _client(webui_app.app) as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}

    mw = next(m for m in webui_app.app.user_middleware if m.cls is BasicAuthMiddleware)
    assert mw is not None


# ---- Phase-0 defaults -----------------------------------------------------
def test_mcp_binds_loopback_by_default(monkeypatch):
    monkeypatch.delenv("GEO_MCP_HOST", raising=False)
    config.reset_settings()
    assert config.get_settings().mcp_host == "127.0.0.1"


def test_default_anthropic_model_is_current(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    config.reset_settings()
    assert config.get_settings().anthropic_model == "claude-sonnet-5-5"
