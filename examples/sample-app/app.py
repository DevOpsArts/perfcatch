"""
Sample FastAPI application for testing perfcatch.
Simulates a service with dependency calls.
"""

import asyncio
import os
import random

import httpx
from fastapi import FastAPI

app = FastAPI(title="Sample App for perfcatch testing")

DEPENDENCY_URL = os.environ.get("DEPENDENCY_URL", "http://httpbin.org")


@app.get("/")
async def root():
    return {"service": "sample-app", "status": "ok"}


@app.get("/compute")
async def compute():
    """Simulates CPU-intensive work."""
    total = sum(i * i for i in range(100_000))
    return {"result": total}


@app.get("/memory")
async def memory_heavy():
    """Simulates memory-intensive work."""
    data = [bytearray(1024) for _ in range(1000)]  # ~1MB allocation
    await asyncio.sleep(0.01)
    return {"allocated_kb": len(data)}


@app.get("/with-dependency")
async def with_dependency():
    """Makes an outbound HTTP call to a dependency service."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{DEPENDENCY_URL}/get", timeout=5.0)
        return {
            "dependency_status": resp.status_code,
            "dependency_latency_ms": resp.elapsed.total_seconds() * 1000,
        }


@app.get("/multi-deps")
async def multi_dependencies():
    """Makes multiple dependency calls in parallel."""
    async with httpx.AsyncClient() as client:
        tasks = [
            client.get(f"{DEPENDENCY_URL}/delay/{random.uniform(0.1, 0.5):.1f}", timeout=5.0),
            client.get(f"{DEPENDENCY_URL}/bytes/{random.randint(100, 10000)}", timeout=5.0),
            client.get(f"{DEPENDENCY_URL}/get", timeout=5.0),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            "dependency_count": len(results),
            "successes": sum(1 for r in results if not isinstance(r, Exception)),
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
