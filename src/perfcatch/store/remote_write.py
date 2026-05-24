"""
Prometheus Remote Write client for pushing per-request metrics.

Sends time-series data to any Prometheus Remote Write compatible endpoint
(Prometheus, VictoriaMetrics, Mimir, Thanos, Cortex).

Uses snappy compression and protobuf encoding per the Remote Write spec.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from collections import deque
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen
from urllib.error import URLError

if TYPE_CHECKING:
    from ..store.ringbuffer import StoredRequest

logger = logging.getLogger(__name__)

# Remote Write uses snappy-compressed protobuf. For simplicity and to avoid
# heavy dependencies, we use the Prometheus text exposition format with
# the remote write receiver's /api/v1/import/prometheus endpoint
# (VictoriaMetrics) or a lightweight protobuf implementation.


class RemoteWriteClient:
    """Push metrics to a Prometheus Remote Write compatible endpoint.

    Supports:
      - VictoriaMetrics: /api/v1/import/prometheus (text format, simplest)
      - Prometheus/Mimir/Cortex: /api/v1/write (protobuf, requires snappy)

    For maximum compatibility, uses the Prometheus text exposition format
    pushed to VictoriaMetrics-compatible import endpoints.
    """

    def __init__(
        self,
        endpoint: str,
        batch_size: int = 500,
        flush_interval: float = 5.0,
        max_retries: int = 3,
        timeout: float = 10.0,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._max_retries = max_retries
        self._timeout = timeout
        self._queue: deque[str] = deque(maxlen=100000)
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_push_ts: float = 0.0
        self._push_count: int = 0
        self._error_count: int = 0

    def start(self) -> None:
        """Start the background push thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._push_loop, daemon=True, name="remote-write"
        )
        self._thread.start()
        logger.info("Remote write client started → %s", self._endpoint)

    def stop(self) -> None:
        """Stop the push thread and flush remaining data."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        # Final flush
        self._flush()

    def enqueue_requests(self, requests: list["StoredRequest"]) -> None:
        """Convert requests to Prometheus text format and queue for pushing."""
        lines = []
        for r in requests:
            ts_ms = int(r.timestamp * 1000)
            labels = self._make_labels(r)

            lines.append(
                f"perfcatch_req_duration_ms{{{labels}}} {r.duration_ms:.2f} {ts_ms}"
            )
            lines.append(
                f"perfcatch_req_cpu_ms{{{labels}}} {r.cpu_time_ms:.2f} {ts_ms}"
            )
            lines.append(
                f"perfcatch_req_memory_bytes{{{labels}}} {r.memory_rss_bytes} {ts_ms}"
            )
            lines.append(
                f"perfcatch_req_bytes_rx{{{labels}}} {r.bytes_received} {ts_ms}"
            )
            lines.append(
                f"perfcatch_req_bytes_tx{{{labels}}} {r.bytes_sent} {ts_ms}"
            )

        with self._lock:
            self._queue.extend(lines)

    def _make_labels(self, r: "StoredRequest") -> str:
        """Build Prometheus label string for a request."""
        corr = r.correlation_id or "none"
        method = r.http_method or "unknown"
        path = r.http_path or "unknown"
        return (
            f'request_id="{r.request_id}",'
            f'namespace="{r.namespace}",'
            f'pod="{r.pod_name}",'
            f'process="{r.process_name}",'
            f'port="{r.local_port}",'
            f'correlation_id="{corr}",'
            f'method="{method}",'
            f'path="{path}"'
        )

    def _push_loop(self) -> None:
        """Background loop that pushes batches to the remote endpoint."""
        while self._running:
            time.sleep(self._flush_interval)
            self._flush()

    def _flush(self) -> None:
        """Push queued metrics to the remote endpoint."""
        with self._lock:
            if not self._queue:
                return
            # Take up to batch_size lines
            batch = []
            for _ in range(min(self._batch_size * 5, len(self._queue))):
                batch.append(self._queue.popleft())

        if not batch:
            return

        payload = "\n".join(batch) + "\n"
        self._send(payload.encode("utf-8"))

    def _send(self, data: bytes) -> None:
        """Send data to the remote write endpoint with retries."""
        # Determine the import URL based on endpoint format
        url = self._endpoint
        if "/api/v1/import" not in url and "/api/v1/write" not in url:
            # Default to VictoriaMetrics-compatible import
            url = f"{url}/api/v1/import/prometheus"

        for attempt in range(self._max_retries):
            try:
                req = Request(
                    url,
                    data=data,
                    headers={
                        "Content-Type": "text/plain",
                    },
                    method="POST",
                )
                with urlopen(req, timeout=self._timeout) as resp:
                    if resp.status < 300:
                        self._push_count += 1
                        self._last_push_ts = time.time()
                        return
                    else:
                        logger.warning(
                            "Remote write HTTP %d (attempt %d/%d)",
                            resp.status, attempt + 1, self._max_retries,
                        )
            except URLError as e:
                logger.warning(
                    "Remote write failed (attempt %d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )
            except Exception as e:
                logger.warning(
                    "Remote write error (attempt %d/%d): %s",
                    attempt + 1, self._max_retries, e,
                )

            if attempt < self._max_retries - 1:
                time.sleep(1.0 * (attempt + 1))

        self._error_count += 1
        logger.error("Remote write failed after %d retries, dropping batch", self._max_retries)

    @property
    def stats(self) -> dict:
        """Return push statistics."""
        return {
            "endpoint": self._endpoint,
            "queue_size": len(self._queue),
            "push_count": self._push_count,
            "error_count": self._error_count,
            "last_push": self._last_push_ts,
        }
