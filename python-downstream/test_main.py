"""Tests for retry logic in the Python downstream service."""

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response

import main
from main import UPSTREAM_URL, app

BASE = f"{UPSTREAM_URL}/api/data"


@pytest_asyncio.fixture
async def client():
    """Set up the shared http_client and a test client for FastAPI."""
    # Create the http_client that main.py uses for upstream calls.
    # respx will intercept requests made by this client.
    main.http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client

    await main.http_client.aclose()
    main.http_client = None


# ── Happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_success_no_retries(client):
    """Successful upstream call — 0 retries."""
    respx.get(BASE).mock(return_value=Response(200, json={"msg": "hello"}))

    resp = await client.get("/")
    data = resp.json()

    assert resp.status_code == 200
    assert data["status"] == "ok"
    assert data["retries"] == 0
    assert data["upstream"] == {"msg": "hello"}
    assert respx.calls.call_count == 1


# ── Transient ConnectError then success ─────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_connect_error_then_success(client):
    """First attempt ConnectError, second succeeds — 1 retry."""
    route = respx.get(BASE)
    route.side_effect = [
        httpx.ConnectError("connection refused"),
        Response(200, json={"msg": "recovered"}),
    ]

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "ok"
    assert data["retries"] == 1
    assert data["upstream"] == {"msg": "recovered"}
    assert respx.calls.call_count == 2


# ── Transient 502 then success ──────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_502_then_success(client):
    """First attempt gets 502, second succeeds — 1 retry."""
    route = respx.get(BASE)
    route.side_effect = [
        Response(502, text="Bad Gateway"),
        Response(200, json={"msg": "ok now"}),
    ]

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "ok"
    assert data["retries"] == 1
    assert respx.calls.call_count == 2


# ── All 3 attempts fail with ConnectError ───────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_all_attempts_connect_error(client):
    """All 3 attempts fail with ConnectError — returns error response."""
    respx.get(BASE).mock(side_effect=httpx.ConnectError("refused"))

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "upstream_unreachable"
    assert data["retries"] == 2  # 3 attempts = 2 retries
    assert "Connection error" in data["error"]
    assert respx.calls.call_count == 3


# ── All 3 attempts return 503 ──────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_all_attempts_server_error(client):
    """All 3 attempts return 503 — returns error after exhausting retries."""
    respx.get(BASE).mock(return_value=Response(503, text="Service Unavailable"))

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "upstream_error"
    assert data["retries"] == 2  # 3 attempts = 2 retries
    assert respx.calls.call_count == 3


# ── 4xx is NOT retried ──────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_404_not_retried(client):
    """404 should NOT be retried — fails immediately."""
    respx.get(BASE).mock(return_value=Response(404, text="Not Found"))

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "upstream_error"
    assert data["retries"] == 0
    assert "404" in data["error"]
    assert respx.calls.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_400_not_retried(client):
    """400 should NOT be retried — fails immediately."""
    respx.get(BASE).mock(return_value=Response(400, text="Bad Request"))

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "upstream_error"
    assert data["retries"] == 0
    assert "400" in data["error"]
    assert respx.calls.call_count == 1


# ── ReadTimeout then success ────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_read_timeout_then_success(client):
    """First attempt times out, second succeeds — 1 retry."""
    route = respx.get(BASE)
    route.side_effect = [
        httpx.ReadTimeout("read timed out"),
        Response(200, json={"msg": "fast this time"}),
    ]

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "ok"
    assert data["retries"] == 1
    assert respx.calls.call_count == 2


# ── All 3 attempts timeout ─────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_all_attempts_timeout(client):
    """All 3 attempts timeout — returns timeout error."""
    respx.get(BASE).mock(side_effect=httpx.ReadTimeout("timed out"))

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "timeout"
    assert data["retries"] == 2
    assert "timeout" in data["error"].lower()
    assert respx.calls.call_count == 3


# ── Mixed failures then success ─────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_mixed_errors_then_success(client):
    """ConnectError, then 503, then 200 — recovers on 3rd attempt."""
    route = respx.get(BASE)
    route.side_effect = [
        httpx.ConnectError("refused"),
        Response(503, text="Unavailable"),
        Response(200, json={"msg": "third time lucky"}),
    ]

    resp = await client.get("/")
    data = resp.json()

    assert data["status"] == "ok"
    assert data["retries"] == 2
    assert respx.calls.call_count == 3


# ── Each 5xx code is retried ───────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_500_is_retried(client):
    """HTTP 500 triggers retry."""
    route = respx.get(BASE)
    route.side_effect = [
        Response(500, text="Internal Server Error"),
        Response(200, json={"msg": "ok"}),
    ]

    resp = await client.get("/")
    assert resp.json()["retries"] == 1
    assert respx.calls.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_504_is_retried(client):
    """HTTP 504 triggers retry."""
    route = respx.get(BASE)
    route.side_effect = [
        Response(504, text="Gateway Timeout"),
        Response(200, json={"msg": "ok"}),
    ]

    resp = await client.get("/")
    assert resp.json()["retries"] == 1
    assert respx.calls.call_count == 2


# ── Health endpoint ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health(client):
    """Health endpoint returns healthy status."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


# ── Response always contains retries field ──────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_response_always_has_retries_field(client):
    """Every response must include the 'retries' field."""
    respx.get(BASE).mock(return_value=Response(200, json={"msg": "hi"}))

    resp = await client.get("/")
    assert "retries" in resp.json()


# ── Elapsed_ms is present and reasonable ────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_elapsed_ms_present(client):
    """Response includes elapsed_ms timing."""
    respx.get(BASE).mock(return_value=Response(200, json={"msg": "hi"}))

    resp = await client.get("/")
    data = resp.json()
    assert "elapsed_ms" in data
    assert isinstance(data["elapsed_ms"], int)
    assert data["elapsed_ms"] >= 0
