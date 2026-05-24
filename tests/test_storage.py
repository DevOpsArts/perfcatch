"""Tests for the storage backend."""

import os
import tempfile
import time

import pytest

from perfcatch.agent.correlator import DependencyCall, RequestProfile
from perfcatch.store.backend import StorageBackend


@pytest.fixture
def storage():
    """Create a temporary storage backend for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    backend = StorageBackend(db_path)
    backend.initialize()
    yield backend
    backend.close()
    os.unlink(db_path)


def _make_profile(
    request_id: str = "req-test-001",
    namespace: str = "default",
    service: str = "my-app",
    pod: str = "my-app-abc123",
    duration_ms: float = 50.0,
    cpu_ms: float = 30.0,
    **kwargs,
) -> RequestProfile:
    """Helper to create a test profile."""
    return RequestProfile(
        request_id=request_id,
        timestamp=time.time(),
        pid=1234,
        tid=1234,
        process_name="python",
        duration_ms=duration_ms,
        cpu_time_ms=cpu_ms,
        memory_rss_bytes=kwargs.get("memory_rss_bytes", 50 * 1024 * 1024),
        bytes_received=kwargs.get("bytes_recv", 512),
        bytes_sent=kwargs.get("bytes_sent", 2048),
        network_bandwidth_bps=kwargs.get("bandwidth", 24000.0),
        local_port=8000,
        remote_ip="10.0.0.1",
        remote_port=54321,
        dependencies=kwargs.get("dependencies", []),
        total_dependency_time_ms=kwargs.get("dep_time", 0),
        pod_name=pod,
        namespace=namespace,
        container_name="app",
        service_name=service,
    )


def test_store_and_query(storage):
    """Profiles can be stored and queried."""
    profiles = [
        _make_profile("req-001", duration_ms=100),
        _make_profile("req-002", duration_ms=50),
        _make_profile("req-003", duration_ms=200),
    ]
    storage.store_profiles(profiles)

    results = storage.query_requests(namespace="default", since_minutes=5)
    assert len(results) == 3


def test_query_filter_by_service(storage):
    """Queries can filter by service name."""
    profiles = [
        _make_profile("req-001", service="service-a"),
        _make_profile("req-002", service="service-b"),
        _make_profile("req-003", service="service-a"),
    ]
    storage.store_profiles(profiles)

    results = storage.query_requests(service="service-a", since_minutes=5)
    assert len(results) == 2


def test_query_filter_by_pod(storage):
    """Queries can filter by pod name."""
    profiles = [
        _make_profile("req-001", pod="pod-1"),
        _make_profile("req-002", pod="pod-2"),
    ]
    storage.store_profiles(profiles)

    results = storage.query_requests(pod="pod-1", since_minutes=5)
    assert len(results) == 1
    assert results[0].pod == "pod-1"


def test_store_with_dependencies(storage):
    """Dependency calls are stored and retrieved."""
    deps = [
        DependencyCall(
            dest_ip="10.0.1.5",
            dest_port=5432,
            duration_ms=15.0,
            bytes_sent=100,
            bytes_recv=500,
            service_name="postgres.db",
        ),
        DependencyCall(
            dest_ip="10.0.1.10",
            dest_port=6379,
            duration_ms=2.0,
            bytes_sent=50,
            bytes_recv=200,
            service_name="redis.cache",
        ),
    ]
    profile = _make_profile("req-deps", dependencies=deps, dep_time=17.0)
    storage.store_profiles([profile])

    results = storage.query_requests(since_minutes=5)
    assert len(results) == 1
    assert results[0].dep_count == 2
    assert results[0].dep_time_ms == 17.0


def test_aggregated_report(storage):
    """Aggregated report computes statistics correctly."""
    profiles = [
        _make_profile(f"req-{i:03d}", duration_ms=10.0 * (i + 1), cpu_ms=5.0 * (i + 1))
        for i in range(10)
    ]
    storage.store_profiles(profiles)

    report = storage.get_aggregated_report(namespace="default", since_minutes=5)
    assert report is not None
    assert report.total_requests == 10
    assert report.avg_duration_ms > 0
    assert report.max_duration_ms == 100.0


def test_aggregated_report_empty(storage):
    """Aggregated report returns None when no data matches."""
    report = storage.get_aggregated_report(namespace="nonexistent", since_minutes=5)
    assert report is None


def test_time_window_filter(storage):
    """Old requests outside the time window are excluded."""
    old_profile = _make_profile("req-old")
    old_profile.timestamp = time.time() - 7200  # 2 hours ago

    new_profile = _make_profile("req-new")

    storage.store_profiles([old_profile, new_profile])

    results = storage.query_requests(since_minutes=60)
    assert len(results) == 1
    assert results[0].request_id == "req-new"
