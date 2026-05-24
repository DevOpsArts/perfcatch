"""Tests for the request correlator."""

import time
from perfcatch.agent.collector import DependencyEvent, MemorySnapshot, RequestEvent
from perfcatch.agent.correlator import RequestCorrelator


def test_handle_request_basic():
    """A simple request without dependencies produces a valid profile."""
    correlator = RequestCorrelator()

    event = RequestEvent(
        start_ns=1000000000,
        duration_ns=50000000,  # 50ms
        cpu_time_ns=30000000,  # 30ms
        pid=1234,
        tid=1234,
        bytes_sent=2048,
        bytes_recv=512,
        local_port=8000,
        remote_port=54321,
        src_ip="10.0.0.1",
        dst_ip="10.0.0.100",
        comm="python",
    )

    profile = correlator.handle_request(event)

    assert profile.request_id.startswith("req-")
    assert profile.duration_ms == 50.0
    assert profile.cpu_time_ms == 30.0
    assert profile.bytes_sent == 2048
    assert profile.bytes_recv == 512
    assert profile.pid == 1234
    assert profile.process_name == "python"
    assert profile.dependencies == []


def test_handle_request_with_dependencies():
    """Dependencies buffered before request are correlated."""
    correlator = RequestCorrelator()

    # Buffer dependency events first
    dep1 = DependencyEvent(
        start_ns=1000000000,
        duration_ns=10000000,  # 10ms
        pid=1234,
        tid=1234,
        bytes_sent=100,
        bytes_recv=500,
        dest_ip="10.0.1.5",
        dest_port=5432,
        protocol=6,
        comm="python",
    )
    dep2 = DependencyEvent(
        start_ns=1010000000,
        duration_ns=5000000,  # 5ms
        pid=1234,
        tid=1234,
        bytes_sent=50,
        bytes_recv=200,
        dest_ip="10.0.1.10",
        dest_port=6379,
        protocol=6,
        comm="python",
    )

    correlator.handle_dependency(dep1)
    correlator.handle_dependency(dep2)

    # Now handle the request
    event = RequestEvent(
        start_ns=1000000000,
        duration_ns=80000000,  # 80ms
        cpu_time_ns=40000000,
        pid=1234,
        tid=1234,
        bytes_sent=2048,
        bytes_recv=512,
        local_port=8000,
        remote_port=54321,
        src_ip="10.0.0.1",
        dst_ip="10.0.0.100",
        comm="python",
    )

    profile = correlator.handle_request(event)

    assert len(profile.dependencies) == 2
    assert profile.dependencies[0].dest_port == 5432
    assert profile.dependencies[0].duration_ms == 10.0
    assert profile.dependencies[1].dest_port == 6379
    assert profile.total_dependency_time_ms == 15.0


def test_drain_completed():
    """Completed profiles are drained correctly."""
    correlator = RequestCorrelator()

    event = RequestEvent(
        start_ns=1000000000,
        duration_ns=50000000,
        cpu_time_ns=30000000,
        pid=100,
        tid=100,
        bytes_sent=1024,
        bytes_recv=256,
        local_port=8000,
        remote_port=12345,
        src_ip="10.0.0.1",
        dst_ip="10.0.0.50",
        comm="app",
    )

    correlator.handle_request(event)
    profiles = correlator.drain_completed()

    assert len(profiles) == 1
    assert profiles[0].pid == 100

    # Second drain should be empty
    assert correlator.drain_completed() == []


def test_memory_baseline():
    """Memory snapshots update the baseline for PIDs."""
    correlator = RequestCorrelator()

    snap = MemorySnapshot(
        timestamp_ns=1000000000,
        pid=1234,
        rss_bytes=50 * 1024 * 1024,  # 50MB
        vm_bytes=200 * 1024 * 1024,
        comm="python",
    )
    correlator.handle_memory(snap)

    event = RequestEvent(
        start_ns=1000000000,
        duration_ns=50000000,
        cpu_time_ns=30000000,
        pid=1234,
        tid=1234,
        bytes_sent=1024,
        bytes_recv=256,
        local_port=8000,
        remote_port=12345,
        src_ip="10.0.0.1",
        dst_ip="10.0.0.50",
        comm="python",
    )

    profile = correlator.handle_request(event)
    assert profile.memory_rss_bytes == 50 * 1024 * 1024


def test_cleanup_stale():
    """Stale dependency events are cleaned up."""
    correlator = RequestCorrelator(window_seconds=0.1)

    old_dep = DependencyEvent(
        start_ns=0,  # Very old timestamp
        duration_ns=1000000,
        pid=999,
        tid=999,
        bytes_sent=0,
        bytes_recv=0,
        dest_ip="10.0.0.1",
        dest_port=80,
        protocol=6,
        comm="old",
    )
    correlator.handle_dependency(old_dep)
    correlator.cleanup_stale()

    # The stale dep should be cleaned
    assert (999, 999) not in correlator._pending_deps


def test_network_bandwidth_calculation():
    """Bandwidth is calculated correctly from bytes and duration."""
    correlator = RequestCorrelator()

    event = RequestEvent(
        start_ns=0,
        duration_ns=1_000_000_000,  # 1 second
        cpu_time_ns=500_000_000,
        pid=1,
        tid=1,
        bytes_sent=1000,
        bytes_recv=2000,
        local_port=8000,
        remote_port=12345,
        src_ip="10.0.0.1",
        dst_ip="10.0.0.2",
        comm="test",
    )

    profile = correlator.handle_request(event)
    # (1000 + 2000) bytes * 8 bits / 1 second = 24000 bps
    assert profile.network_bandwidth_bps == 24000.0
