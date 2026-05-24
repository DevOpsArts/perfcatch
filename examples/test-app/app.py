"""Simple Flask test app for perfcatch monitoring demo."""

import time
import math
import os
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "test-app"})


@app.route("/fast")
def fast_endpoint():
    """Fast endpoint - minimal work."""
    return jsonify({"message": "hello", "latency": "minimal"})


@app.route("/compute")
def compute():
    """CPU-intensive endpoint."""
    start = time.time()
    total = sum(math.sqrt(i) for i in range(500_000))
    elapsed = time.time() - start
    return jsonify({"result": total, "compute_time_ms": elapsed * 1000})


@app.route("/memory")
def memory():
    """Memory-intensive endpoint."""
    start = time.time()
    data = [bytearray(4096) for _ in range(500)]  # ~2MB
    elapsed = time.time() - start
    return jsonify({"allocated_kb": len(data) * 4, "alloc_time_ms": elapsed * 1000})


@app.route("/slow")
def slow():
    """Simulates a slow I/O-bound endpoint."""
    time.sleep(0.2)
    return jsonify({"message": "done", "sleep_ms": 200})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
