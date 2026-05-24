"""
Request correlator - combines eBPF events into complete request profiles.

Takes raw events from the collector and correlates them into a single
request profile that includes: duration, CPU time, memory delta,
network I/O, and dependency calls.
"""

from __future__ import annotations

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from .collector import DependencyEvent, MemorySnapshot, RequestEvent


@dataclass
class DependencyCall:
    """A single outbound dependency call within a request."""

    dest_ip: str
    dest_port: int
    duration_ms: float
    bytes_sent: int
    bytes_recv: int
    service_name: str = ""  # Resolved from K8s metadata


@dataclass
class RequestProfile:
    """Complete resource profile for a single request."""

    request_id: str
    timestamp: float
    pid: int
    tid: int
    process_name: str

    # Timing
    duration_ms: float
    cpu_time_ms: float

    # Memory
    memory_rss_bytes: int = 0
    memory_delta_bytes: int = 0

    # Network
    bytes_received: int = 0
    bytes_sent: int = 0
    network_bandwidth_bps: float = 0.0

    # Connection info
    local_port: int = 0
    remote_ip: str = ""
    remote_port: int = 0

    # Dependencies
    dependencies: list[DependencyCall] = field(default_factory=list)
    total_dependency_time_ms: float = 0.0

    # Kubernetes metadata (populated later)
    pod_name: str = ""
    namespace: str = ""
    container_name: str = ""
    service_name: str = ""

    # HTTP context (from eBPF header capture)
    correlation_id: str = ""  # From X-Correlation-ID, X-Request-ID, traceparent, etc.
    http_method: str = ""     # GET, POST, etc.
    http_path: str = ""       # Request path


class RequestCorrelator:
    """Correlates raw eBPF events into complete request profiles."""

    def __init__(self, window_seconds: float = 5.0):
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        self._request_counter = 0

        # Pending dependency events keyed by (pid, tid)
        self._pending_deps: dict[tuple[int, int], list[DependencyEvent]] = defaultdict(list)

        # Memory baselines per pid
        self._memory_baseline: dict[int, int] = {}

        # Completed profiles ready for storage
        self._completed: list[RequestProfile] = []

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"req-{int(time.time() * 1000)}-{self._request_counter:06d}"

    def handle_request(self, event: RequestEvent) -> RequestProfile:
        """Process a completed request event and build its profile."""
        with self._lock:
            request_id = self._next_request_id()

            duration_ms = event.duration_ns / 1_000_000
            cpu_time_ms = event.cpu_time_ns / 1_000_000

            # Calculate network bandwidth (bytes per second)
            total_bytes = event.bytes_sent + event.bytes_recv
            duration_s = event.duration_ns / 1_000_000_000 if event.duration_ns > 0 else 1
            bandwidth_bps = (total_bytes * 8) / duration_s

            # Gather dependency calls for this (pid, tid)
            key = (event.pid, event.tid)
            dep_events = self._pending_deps.pop(key, [])
            dependencies = []
            total_dep_time = 0.0

            for dep in dep_events:
                dep_duration_ms = dep.duration_ns / 1_000_000
                total_dep_time += dep_duration_ms
                dependencies.append(
                    DependencyCall(
                        dest_ip=dep.dest_ip,
                        dest_port=dep.dest_port,
                        duration_ms=dep_duration_ms,
                        bytes_sent=dep.bytes_sent,
                        bytes_recv=dep.bytes_recv,
                    )
                )

            # Memory from userspace /proc read
            mem_rss = event.mem_rss_bytes if hasattr(event, 'mem_rss_bytes') else 0

            profile = RequestProfile(
                request_id=request_id,
                timestamp=time.time(),
                pid=event.pid,
                tid=event.tid,
                process_name=event.comm,
                duration_ms=duration_ms,
                cpu_time_ms=cpu_time_ms,
                memory_rss_bytes=mem_rss,
                memory_delta_bytes=0,
                bytes_received=event.bytes_recv,
                bytes_sent=event.bytes_sent,
                network_bandwidth_bps=bandwidth_bps,
                local_port=event.local_port,
                remote_ip=event.src_ip,
                remote_port=event.remote_port,
                dependencies=dependencies,
                total_dependency_time_ms=total_dep_time,
                correlation_id=event.correlation_id,
                http_method=event.http_method,
                http_path=event.http_path,
            )

            self._completed.append(profile)
            return profile

    def handle_dependency(self, event: DependencyEvent) -> None:
        """Buffer a dependency event for correlation with its parent request."""
        with self._lock:
            key = (event.pid, event.tid)
            self._pending_deps[key].append(event)

    def handle_memory(self, snapshot: MemorySnapshot) -> None:
        """Update memory baseline for a process."""
        with self._lock:
            self._memory_baseline[snapshot.pid] = snapshot.rss_bytes

    def drain_completed(self) -> list[RequestProfile]:
        """Return and clear all completed profiles."""
        with self._lock:
            profiles = self._completed
            self._completed = []
            return profiles

    def cleanup_stale(self) -> None:
        """Remove dependency events older than the correlation window."""
        cutoff_ns = int((time.time() - self._window_seconds) * 1_000_000_000)
        with self._lock:
            for key in list(self._pending_deps.keys()):
                self._pending_deps[key] = [
                    d for d in self._pending_deps[key] if d.start_ns > cutoff_ns
                ]
                if not self._pending_deps[key]:
                    del self._pending_deps[key]
