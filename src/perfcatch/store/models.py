"""
Data models for request profiles and reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class StoredProfile:
    """A request profile as stored in the database."""

    request_id: str
    timestamp: datetime
    pid: int
    tid: int
    process_name: str
    pod_name: str
    namespace: str
    container_name: str
    service_name: str

    # Metrics
    duration_ms: float
    cpu_time_ms: float
    memory_rss_bytes: int
    memory_delta_bytes: int
    bytes_received: int
    bytes_sent: int
    network_bandwidth_bps: float
    local_port: int
    remote_ip: str
    remote_port: int

    # Dependencies (stored as JSON)
    dependencies: list[dict] = field(default_factory=list)
    total_dependency_time_ms: float = 0.0


@dataclass
class AggregatedReport:
    """Aggregated statistics for a namespace/application."""

    namespace: str
    service_name: str
    time_window_start: datetime
    time_window_end: datetime
    total_requests: int

    # Duration stats
    avg_duration_ms: float
    p50_duration_ms: float
    p95_duration_ms: float
    p99_duration_ms: float
    max_duration_ms: float

    # CPU stats
    avg_cpu_ms: float
    max_cpu_ms: float
    total_cpu_ms: float

    # Memory stats
    avg_memory_bytes: int
    max_memory_bytes: int

    # Network stats
    total_bytes_sent: int
    total_bytes_recv: int
    avg_bandwidth_bps: float

    # Dependency stats
    avg_dep_time_ms: float
    max_dep_time_ms: float
    dep_call_count: int
    top_dependencies: list[dict] = field(default_factory=list)


@dataclass
class RequestDetail:
    """Detailed view of a single request for reporting."""

    request_id: str
    timestamp: str
    pod: str
    service: str
    duration_ms: float
    cpu_ms: float
    memory_mb: float
    net_in_kb: float
    net_out_kb: float
    bandwidth_mbps: float
    dep_count: int
    dep_time_ms: float
    dependencies: list[dict] = field(default_factory=list)
