import os
import time

import httpx
from fastapi import FastAPI

app = FastAPI(title="Python Downstream Service")

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://go-upstream:7000")


@app.get("/")
async def call_upstream():
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(f"{UPSTREAM_URL}/api/data")
            resp.raise_for_status()
            upstream_data = resp.json()
    except httpx.ConnectError as e:
        elapsed = round((time.time() - start) * 1000)
        return {
            "source": "python-downstream",
            "error": f"Connection error: {e}",
            "elapsed_ms": elapsed,
            "status": "upstream_unreachable",
        }
    except httpx.ReadTimeout:
        elapsed = round((time.time() - start) * 1000)
        return {
            "source": "python-downstream",
            "error": "Upstream read timeout (5s)",
            "elapsed_ms": elapsed,
            "status": "timeout",
        }
    except httpx.HTTPStatusError as e:
        elapsed = round((time.time() - start) * 1000)
        return {
            "source": "python-downstream",
            "error": f"Upstream returned {e.response.status_code}",
            "elapsed_ms": elapsed,
            "status": "upstream_error",
        }

    elapsed = round((time.time() - start) * 1000)
    return {
        "source": "python-downstream",
        "upstream": upstream_data,
        "elapsed_ms": elapsed,
        "status": "ok",
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}
