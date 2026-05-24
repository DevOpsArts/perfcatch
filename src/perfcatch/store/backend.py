"""
Storage backend - SQLite-based persistence for request profiles.

Stores per-request metrics and provides query capabilities for
the CLI report tool.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..agent.correlator import RequestProfile
from .models import AggregatedReport, RequestDetail, StoredProfile


class StorageBackend:
    """SQLite storage for request profiles."""

    def __init__(self, db_path: str = "/var/lib/perfcatch/data.db"):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        """Create database and tables."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self) -> None:
        """Create schema for request profiles."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS request_profiles (
                request_id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                pid INTEGER NOT NULL,
                tid INTEGER NOT NULL,
                process_name TEXT,
                pod_name TEXT,
                namespace TEXT,
                container_name TEXT,
                service_name TEXT,
                duration_ms REAL NOT NULL,
                cpu_time_ms REAL NOT NULL,
                memory_rss_bytes INTEGER DEFAULT 0,
                memory_delta_bytes INTEGER DEFAULT 0,
                bytes_received INTEGER DEFAULT 0,
                bytes_sent INTEGER DEFAULT 0,
                network_bandwidth_bps REAL DEFAULT 0,
                local_port INTEGER,
                remote_ip TEXT,
                remote_port INTEGER,
                total_dependency_time_ms REAL DEFAULT 0,
                dependencies_json TEXT DEFAULT '[]',
                correlation_id TEXT DEFAULT NULL,
                http_method TEXT DEFAULT NULL,
                http_path TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_profiles_timestamp
                ON request_profiles(timestamp);
            CREATE INDEX IF NOT EXISTS idx_profiles_namespace
                ON request_profiles(namespace);
            CREATE INDEX IF NOT EXISTS idx_profiles_service
                ON request_profiles(service_name);
            CREATE INDEX IF NOT EXISTS idx_profiles_pod
                ON request_profiles(pod_name);
            CREATE INDEX IF NOT EXISTS idx_profiles_ns_svc
                ON request_profiles(namespace, service_name);

            CREATE TABLE IF NOT EXISTS dependency_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                dest_ip TEXT,
                dest_port INTEGER,
                service_name TEXT,
                duration_ms REAL,
                bytes_sent INTEGER DEFAULT 0,
                bytes_recv INTEGER DEFAULT 0,
                FOREIGN KEY (request_id) REFERENCES request_profiles(request_id)
            );

            CREATE INDEX IF NOT EXISTS idx_deps_request
                ON dependency_calls(request_id);
            CREATE INDEX IF NOT EXISTS idx_deps_service
                ON dependency_calls(service_name);
        """)
        # Migrate existing databases: add new columns if missing
        try:
            self._conn.execute("ALTER TABLE request_profiles ADD COLUMN correlation_id TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            self._conn.execute("ALTER TABLE request_profiles ADD COLUMN http_method TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE request_profiles ADD COLUMN http_path TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def store_profiles(self, profiles: list[RequestProfile]) -> None:
        """Batch insert request profiles."""
        with self._lock:
            for profile in profiles:
                deps_json = json.dumps([
                    {
                        "dest_ip": d.dest_ip,
                        "dest_port": d.dest_port,
                        "service_name": d.service_name,
                        "duration_ms": d.duration_ms,
                        "bytes_sent": d.bytes_sent,
                        "bytes_recv": d.bytes_recv,
                    }
                    for d in profile.dependencies
                ])

                self._conn.execute(
                    """INSERT OR REPLACE INTO request_profiles
                    (request_id, timestamp, pid, tid, process_name, pod_name,
                     namespace, container_name, service_name, duration_ms,
                     cpu_time_ms, memory_rss_bytes, memory_delta_bytes,
                     bytes_received, bytes_sent, network_bandwidth_bps,
                     local_port, remote_ip, remote_port,
                     total_dependency_time_ms, dependencies_json,
                     correlation_id, http_method, http_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        profile.request_id,
                        profile.timestamp,
                        profile.pid,
                        profile.tid,
                        profile.process_name,
                        profile.pod_name,
                        profile.namespace,
                        profile.container_name,
                        profile.service_name,
                        profile.duration_ms,
                        profile.cpu_time_ms,
                        profile.memory_rss_bytes,
                        profile.memory_delta_bytes,
                        profile.bytes_received,
                        profile.bytes_sent,
                        profile.network_bandwidth_bps,
                        profile.local_port,
                        profile.remote_ip,
                        profile.remote_port,
                        profile.total_dependency_time_ms,
                        deps_json,
                        profile.correlation_id or None,
                        profile.http_method or None,
                        profile.http_path or None,
                    ),
                )

                # Store individual dependency calls
                for dep in profile.dependencies:
                    self._conn.execute(
                        """INSERT INTO dependency_calls
                        (request_id, dest_ip, dest_port, service_name,
                         duration_ms, bytes_sent, bytes_recv)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            profile.request_id,
                            dep.dest_ip,
                            dep.dest_port,
                            dep.service_name,
                            dep.duration_ms,
                            dep.bytes_sent,
                            dep.bytes_recv,
                        ),
                    )

            self._conn.commit()

    def query_requests(
        self,
        namespace: str | None = None,
        service: str | None = None,
        pod: str | None = None,
        since_minutes: int = 60,
        limit: int = 100,
    ) -> list[RequestDetail]:
        """Query request profiles with filters."""
        cutoff = datetime.now().timestamp() - (since_minutes * 60)

        conditions = ["timestamp > ?"]
        params: list[Any] = [cutoff]

        if namespace:
            conditions.append("namespace = ?")
            params.append(namespace)
        if service:
            conditions.append("service_name = ?")
            params.append(service)
        if pod:
            conditions.append("pod_name = ?")
            params.append(pod)

        where = " AND ".join(conditions)
        params.append(limit)

        cursor = self._conn.execute(
            f"""SELECT request_id, timestamp, pod_name, service_name,
                       duration_ms, cpu_time_ms, memory_rss_bytes,
                       bytes_received, bytes_sent, network_bandwidth_bps,
                       total_dependency_time_ms, dependencies_json
                FROM request_profiles
                WHERE {where}
                ORDER BY timestamp DESC
                LIMIT ?""",
            params,
        )

        results = []
        for row in cursor.fetchall():
            deps = json.loads(row[11]) if row[11] else []
            results.append(
                RequestDetail(
                    request_id=row[0],
                    timestamp=datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M:%S"),
                    pod=row[2] or "unknown",
                    service=row[3] or "unknown",
                    duration_ms=row[4],
                    cpu_ms=row[5],
                    memory_mb=row[6] / (1024 * 1024) if row[6] else 0,
                    net_in_kb=row[7] / 1024 if row[7] else 0,
                    net_out_kb=row[8] / 1024 if row[8] else 0,
                    bandwidth_mbps=row[9] / 1_000_000 if row[9] else 0,
                    dep_count=len(deps),
                    dep_time_ms=row[10],
                    dependencies=deps,
                )
            )
        return results

    def get_aggregated_report(
        self,
        namespace: str | None = None,
        service: str | None = None,
        since_minutes: int = 60,
    ) -> AggregatedReport | None:
        """Generate aggregated statistics."""
        cutoff = datetime.now().timestamp() - (since_minutes * 60)

        conditions = ["timestamp > ?"]
        params: list[Any] = [cutoff]

        if namespace:
            conditions.append("namespace = ?")
            params.append(namespace)
        if service:
            conditions.append("service_name = ?")
            params.append(service)

        where = " AND ".join(conditions)

        cursor = self._conn.execute(
            f"""SELECT
                COUNT(*) as total,
                AVG(duration_ms), MAX(duration_ms),
                AVG(cpu_time_ms), MAX(cpu_time_ms), SUM(cpu_time_ms),
                AVG(memory_rss_bytes), MAX(memory_rss_bytes),
                SUM(bytes_sent), SUM(bytes_received),
                AVG(network_bandwidth_bps),
                AVG(total_dependency_time_ms), MAX(total_dependency_time_ms)
            FROM request_profiles
            WHERE {where}""",
            params,
        )

        row = cursor.fetchone()
        if not row or row[0] == 0:
            return None

        # Get percentiles
        percentile_cursor = self._conn.execute(
            f"""SELECT duration_ms FROM request_profiles
                WHERE {where}
                ORDER BY duration_ms""",
            params,
        )
        durations = [r[0] for r in percentile_cursor.fetchall()]
        total = len(durations)

        p50 = durations[int(total * 0.5)] if total > 0 else 0
        p95 = durations[int(total * 0.95)] if total > 0 else 0
        p99 = durations[int(total * 0.99)] if total > 0 else 0

        # Top dependencies
        dep_cursor = self._conn.execute(
            f"""SELECT service_name, COUNT(*) as cnt,
                       AVG(duration_ms), SUM(bytes_sent + bytes_recv)
                FROM dependency_calls dc
                JOIN request_profiles rp ON dc.request_id = rp.request_id
                WHERE rp.{where.replace('timestamp', 'rp.timestamp').replace('namespace', 'rp.namespace').replace('service_name', 'rp.service_name')}
                GROUP BY dc.service_name
                ORDER BY cnt DESC
                LIMIT 10""",
            params,
        )
        top_deps = [
            {
                "service": r[0] or "unknown",
                "call_count": r[1],
                "avg_duration_ms": round(r[2], 2),
                "total_bytes": r[3],
            }
            for r in dep_cursor.fetchall()
        ]

        # Dependency count
        dep_count_cursor = self._conn.execute(
            f"""SELECT COUNT(*) FROM dependency_calls dc
                JOIN request_profiles rp ON dc.request_id = rp.request_id
                WHERE rp.{where.replace('timestamp', 'rp.timestamp').replace('namespace', 'rp.namespace').replace('service_name', 'rp.service_name')}""",
            params,
        )
        dep_count = dep_count_cursor.fetchone()[0]

        return AggregatedReport(
            namespace=namespace or "*",
            service_name=service or "*",
            time_window_start=datetime.fromtimestamp(cutoff),
            time_window_end=datetime.now(),
            total_requests=row[0],
            avg_duration_ms=round(row[1] or 0, 2),
            p50_duration_ms=round(p50, 2),
            p95_duration_ms=round(p95, 2),
            p99_duration_ms=round(p99, 2),
            max_duration_ms=round(row[2] or 0, 2),
            avg_cpu_ms=round(row[3] or 0, 2),
            max_cpu_ms=round(row[4] or 0, 2),
            total_cpu_ms=round(row[5] or 0, 2),
            avg_memory_bytes=int(row[6] or 0),
            max_memory_bytes=int(row[7] or 0),
            total_bytes_sent=int(row[8] or 0),
            total_bytes_recv=int(row[9] or 0),
            avg_bandwidth_bps=round(row[10] or 0, 2),
            avg_dep_time_ms=round(row[11] or 0, 2),
            max_dep_time_ms=round(row[12] or 0, 2),
            dep_call_count=dep_count,
            top_dependencies=top_deps,
        )

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
