import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    retry_if_result,
    stop_after_attempt,
    wait_fixed,
)

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://go-upstream:7000")

http_client: httpx.AsyncClient = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    yield
    await http_client.aclose()


app = FastAPI(title="Python Downstream Service", lifespan=lifespan)

RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


def _is_server_error(response: httpx.Response) -> bool:
    return response.status_code in RETRYABLE_STATUS_CODES


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(0.5),
    retry=(
        retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout))
        | retry_if_result(_is_server_error)
    ),
    reraise=True,
)
async def _call_upstream(attempts: list[int]) -> httpx.Response:
    attempts[0] += 1
    return await http_client.get(f"{UPSTREAM_URL}/api/data")


@app.get("/")
async def call_upstream():
    start = time.time()
    attempts = [0]
    try:
        resp = await _call_upstream(attempts)
        retries = attempts[0] - 1
        resp.raise_for_status()
        upstream_data = resp.json()
    except RetryError:
        retries = attempts[0] - 1
        elapsed = round((time.time() - start) * 1000)
        return {
            "source": "python-downstream",
            "error": "Upstream returned server error after retries",
            "elapsed_ms": elapsed,
            "status": "upstream_error",
            "retries": retries,
        }
    except httpx.ConnectError as e:
        retries = attempts[0] - 1
        elapsed = round((time.time() - start) * 1000)
        return {
            "source": "python-downstream",
            "error": f"Connection error: {e}",
            "elapsed_ms": elapsed,
            "status": "upstream_unreachable",
            "retries": retries,
        }
    except httpx.ReadTimeout:
        retries = attempts[0] - 1
        elapsed = round((time.time() - start) * 1000)
        return {
            "source": "python-downstream",
            "error": "Upstream read timeout (5s)",
            "elapsed_ms": elapsed,
            "status": "timeout",
            "retries": retries,
        }
    except httpx.HTTPStatusError as e:
        retries = attempts[0] - 1
        elapsed = round((time.time() - start) * 1000)
        return {
            "source": "python-downstream",
            "error": f"Upstream returned {e.response.status_code}",
            "elapsed_ms": elapsed,
            "status": "upstream_error",
            "retries": retries,
        }

    elapsed = round((time.time() - start) * 1000)
    return {
        "source": "python-downstream",
        "upstream": upstream_data,
        "elapsed_ms": elapsed,
        "status": "ok",
        "retries": retries,
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}
