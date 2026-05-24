"""Tests for CLI report commands (smoke tests)."""

import os
import tempfile
import time

from click.testing import CliRunner

from perfcatch.agent.correlator import RequestProfile
from perfcatch.cli.main import cli
from perfcatch.store.backend import StorageBackend


def _seed_db(db_path: str, count: int = 5) -> None:
    """Seed a test database with sample profiles."""
    storage = StorageBackend(db_path)
    storage.initialize()

    profiles = []
    for i in range(count):
        profiles.append(
            RequestProfile(
                request_id=f"req-test-{i:03d}",
                timestamp=time.time() - (i * 60),
                pid=1000 + i,
                tid=1000 + i,
                process_name="python",
                duration_ms=10.0 * (i + 1),
                cpu_time_ms=5.0 * (i + 1),
                memory_rss_bytes=(10 + i) * 1024 * 1024,
                bytes_received=512 * (i + 1),
                bytes_sent=1024 * (i + 1),
                network_bandwidth_bps=10000.0 * (i + 1),
                local_port=8000,
                remote_ip=f"10.0.0.{i + 1}",
                remote_port=50000 + i,
                dependencies=[],
                total_dependency_time_ms=0,
                pod_name=f"my-app-pod-{i}",
                namespace="test-ns",
                container_name="app",
                service_name="my-app",
            )
        )

    storage.store_profiles(profiles)
    storage.close()


def test_report_command():
    """Report command runs without errors."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _seed_db(db_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--db", db_path, "report", "-n", "test-ns"])
        assert result.exit_code == 0
        assert "my-app" in result.output or "Per-Request" in result.output
    finally:
        os.unlink(db_path)


def test_services_command():
    """Services command lists monitored services."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _seed_db(db_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--db", db_path, "services"])
        assert result.exit_code == 0
        assert "my-app" in result.output
    finally:
        os.unlink(db_path)


def test_detail_command_not_found():
    """Detail command handles missing request gracefully."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _seed_db(db_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--db", db_path, "detail", "nonexistent"])
        assert result.exit_code == 0
        assert "not found" in result.output
    finally:
        os.unlink(db_path)


def test_detail_command_found():
    """Detail command shows request details."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _seed_db(db_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["--db", db_path, "detail", "req-test-000"])
        assert result.exit_code == 0
        assert "req-test-000" in result.output
    finally:
        os.unlink(db_path)


def test_alerts_no_threshold_breaches():
    """Alerts command shows no results when thresholds aren't exceeded."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _seed_db(db_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--db", db_path, "alerts", "-n", "test-ns", "--threshold-duration", "9999"],
        )
        assert result.exit_code == 0
        assert "No requests exceeding" in result.output
    finally:
        os.unlink(db_path)


def test_alerts_with_breaches():
    """Alerts command flags requests exceeding threshold."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _seed_db(db_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--db", db_path, "alerts", "-n", "test-ns", "--threshold-duration", "25"],
        )
        assert result.exit_code == 0
        # Requests with duration > 25ms should be flagged
        assert "Alerts" in result.output
    finally:
        os.unlink(db_path)
