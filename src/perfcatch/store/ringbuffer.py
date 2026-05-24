"""
In-memory ring buffer storage backend for high-throughput request profiles.

Replaces SQLite with a thread-safe, bounded deque. Designed for 500+ req/s
with zero disk I/O. Prometheus is the long-term time-series store.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterator

from ..agent.correlator import RequestProfile


@dataclass
class StoredRequest:
    """A completed request stored in the ring buffer."""

    request_id: str
    timestamp: float
    pid: int
    tid: int
    process_name: str
    pod_name: str
    namespace: str
    container_name: str
    service_name: str
    local_port: int
    remote_ip: str
    remote_port: int
    duration_ms: float
    cpu_time_ms: float
    memory_rss_bytes: int
    memory_delta_bytes: int
    bytes_received: int
    bytes_sent: int
    network_bandwidth_bps: float
    total_dependency_time_ms: float
    dependencies: list[dict] = field(default_factory=list)
    correlation_id: str | None = None
    http_method: str | None = None
    http_path: str | None = None


class RingBufferStore:
    """Thread-safe in-memory ring buffer for request profiles.

    At 500 req/s, a maxlen of 50000 gives ~100s of history.
    Memory usage: ~50MB for 50K entries (1KB per request avg).
    """

    def __init__(self, maxlen: int = 50000):
        self._buffer: deque[StoredRequest] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._correlated_index: dict[str, list[StoredRequest]] = {}
        self._correlated_lock = threading.Lock()
        # Separate buffer for correlated requests (longer retention for Grafana)
        self._correlated_buffer: deque[StoredRequest] = deque(maxlen=10000)

    def store_profiles(self, profiles: list[RequestProfile]) -> None:
        """Store completed request profiles into the ring buffer."""
        now = time.time()
        entries = []
        for p in profiles:
            entry = StoredRequest(
                request_id=p.request_id,
                timestamp=p.timestamp,
                pid=p.pid,
                tid=p.tid,
                process_name=p.process_name,
                pod_name=p.pod_name or "",
                namespace=p.namespace or "",
                container_name=p.container_name or "",
                service_name=p.service_name or "",
                local_port=p.local_port,
                remote_ip=p.remote_ip or "",
                remote_port=p.remote_port,
                duration_ms=p.duration_ms,
                cpu_time_ms=p.cpu_time_ms,
                memory_rss_bytes=p.memory_rss_bytes,
                memory_delta_bytes=p.memory_delta_bytes,
                bytes_received=p.bytes_received,
                bytes_sent=p.bytes_sent,
                network_bandwidth_bps=p.network_bandwidth_bps,
                total_dependency_time_ms=p.total_dependency_time_ms,
                dependencies=[
                    {
                        "dest_ip": d.dest_ip,
                        "dest_port": d.dest_port,
                        "service_name": d.service_name,
                        "duration_ms": d.duration_ms,
                        "bytes_sent": d.bytes_sent,
                        "bytes_recv": d.bytes_recv,
                    }
                    for d in p.dependencies
                ],
                correlation_id=p.correlation_id or None,
                http_method=p.http_method or None,
                http_path=p.http_path or None,
            )
            entries.append(entry)

        with self._lock:
            self._buffer.extend(entries)

        # Also store correlated entries separately (longer retention for Grafana)
        correlated = [e for e in entries if e.correlation_id]
        if correlated:
            with self._correlated_lock:
                self._correlated_buffer.extend(correlated)

    def query(
        self,
        namespace: str | None = None,
        service: str | None = None,
        pod: str | None = None,
        process: str | None = None,
        port: int | None = None,
        correlation_id: str | None = None,
        since_seconds: float = 300,
        limit: int = 200,
    ) -> list[StoredRequest]:
        """Query requests with filters. Returns newest first."""
        cutoff = time.time() - since_seconds
        results = []

        with self._lock:
            # Iterate in reverse (newest first)
            for entry in reversed(self._buffer):
                if entry.timestamp < cutoff:
                    break
                if namespace and entry.namespace != namespace:
                    continue
                if service and entry.service_name != service:
                    continue
                if pod and entry.pod_name != pod:
                    continue
                if process and entry.process_name != process:
                    continue
                if port and entry.local_port != port:
                    continue
                if correlation_id and entry.correlation_id != correlation_id:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break

        return results

    def get_correlated(self, since_seconds: float = 10800, limit: int = 200) -> list[StoredRequest]:
        """Get correlated requests (longer lookback for Grafana table)."""
        cutoff = time.time() - since_seconds
        results = []

        with self._correlated_lock:
            for entry in reversed(self._correlated_buffer):
                if entry.timestamp < cutoff:
                    break
                results.append(entry)
                if len(results) >= limit:
                    break

        return results

    def get_uncorrelated_recent(self, since_seconds: float = 300, limit: int = 50) -> list[StoredRequest]:
        """Get recent uncorrelated requests."""
        cutoff = time.time() - since_seconds
        results = []

        with self._lock:
            for entry in reversed(self._buffer):
                if entry.timestamp < cutoff:
                    break
                if entry.correlation_id:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break

        return results

    def get_aggregates(self, since_seconds: float = 300) -> dict[tuple, dict]:
        """Get per-service aggregate stats for Prometheus metrics.

        Returns: {(namespace, pod, process, port): {count, sum_duration, max_duration, ...}}
        """
        cutoff = time.time() - since_seconds
        aggs: dict[tuple, dict] = {}

        with self._lock:
            for entry in reversed(self._buffer):
                if entry.timestamp < cutoff:
                    break
                key = (
                    entry.namespace or "unknown",
                    entry.pod_name or "unknown",
                    entry.process_name or "unknown",
                    entry.local_port or 0,
                )
                if key not in aggs:
                    aggs[key] = {
                        "count": 0,
                        "sum_duration_ms": 0.0,
                        "max_duration_ms": 0.0,
                        "sum_cpu_ms": 0.0,
                        "max_cpu_ms": 0.0,
                        "sum_rss": 0,
                        "total_bytes_rx": 0,
                        "total_bytes_tx": 0,
                        "durations": [],
                    }
                a = aggs[key]
                a["count"] += 1
                a["sum_duration_ms"] += entry.duration_ms
                a["max_duration_ms"] = max(a["max_duration_ms"], entry.duration_ms)
                a["sum_cpu_ms"] += entry.cpu_time_ms
                a["max_cpu_ms"] = max(a["max_cpu_ms"], entry.cpu_time_ms)
                a["sum_rss"] += entry.memory_rss_bytes
                a["total_bytes_rx"] += entry.bytes_received
                a["total_bytes_tx"] += entry.bytes_sent
                a["durations"].append(entry.duration_ms)

        return aggs

    def get_all_for_export(self, since_seconds: float = 60) -> list[StoredRequest]:
        """Get all requests for remote write export."""
        cutoff = time.time() - since_seconds
        results = []

        with self._lock:
            for entry in reversed(self._buffer):
                if entry.timestamp < cutoff:
                    break
                results.append(entry)

        return results

    @property
    def size(self) -> int:
        """Current number of entries in the buffer."""
        return len(self._buffer)

    @property
    def maxlen(self) -> int:
        """Maximum capacity of the buffer."""
        return self._buffer.maxlen
