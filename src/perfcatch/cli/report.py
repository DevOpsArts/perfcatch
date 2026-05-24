"""
Report generation - queries data and formats output.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..store.backend import StorageBackend

console = Console()


def generate_report(
    db_path: str,
    namespace: str | None = None,
    service: str | None = None,
    pod: str | None = None,
    since_minutes: int = 60,
    limit: int = 50,
    sort_by: str = "duration",
) -> None:
    """Generate and display the main aggregated + per-request report."""
    storage = StorageBackend(db_path)
    storage.initialize()

    # Aggregated summary
    agg = storage.get_aggregated_report(
        namespace=namespace, service=service, since_minutes=since_minutes
    )

    if agg:
        _print_summary_panel(agg)
        console.print()

    # Per-request table
    requests = storage.query_requests(
        namespace=namespace,
        service=service,
        pod=pod,
        since_minutes=since_minutes,
        limit=limit,
    )

    if not requests:
        console.print("[yellow]No requests found matching the filter criteria.[/yellow]")
        storage.close()
        return

    # Sort
    sort_keys = {
        "duration": lambda r: r.duration_ms,
        "cpu": lambda r: r.cpu_ms,
        "memory": lambda r: r.memory_mb,
        "network": lambda r: r.net_in_kb + r.net_out_kb,
        "deps": lambda r: r.dep_time_ms,
    }
    requests.sort(key=sort_keys.get(sort_by, sort_keys["duration"]), reverse=True)

    _print_request_table(requests)
    storage.close()


def _print_summary_panel(agg) -> None:
    """Print the aggregated summary panel."""
    title = f"Summary: {agg.namespace}/{agg.service_name}"
    window = (
        f"{agg.time_window_start.strftime('%H:%M')} - "
        f"{agg.time_window_end.strftime('%H:%M')}"
    )

    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column("Metric", style="bold cyan")
    summary_table.add_column("Value", style="white")

    summary_table.add_row("Total Requests", str(agg.total_requests))
    summary_table.add_row("Time Window", window)
    summary_table.add_row("", "")

    # Duration
    summary_table.add_row(
        "Duration (avg/p50/p95/p99/max)",
        f"{agg.avg_duration_ms:.1f} / {agg.p50_duration_ms:.1f} / "
        f"{agg.p95_duration_ms:.1f} / {agg.p99_duration_ms:.1f} / "
        f"{agg.max_duration_ms:.1f} ms",
    )

    # CPU
    summary_table.add_row(
        "CPU Time (avg/max/total)",
        f"{agg.avg_cpu_ms:.1f} / {agg.max_cpu_ms:.1f} / {agg.total_cpu_ms:.1f} ms",
    )

    # Memory
    summary_table.add_row(
        "Memory RSS (avg/max)",
        f"{agg.avg_memory_bytes / 1024 / 1024:.1f} / "
        f"{agg.max_memory_bytes / 1024 / 1024:.1f} MB",
    )

    # Network
    summary_table.add_row(
        "Network (sent/recv)",
        f"{agg.total_bytes_sent / 1024:.1f} KB / {agg.total_bytes_recv / 1024:.1f} KB",
    )
    summary_table.add_row(
        "Avg Bandwidth",
        f"{agg.avg_bandwidth_bps / 1_000_000:.2f} Mbps",
    )

    # Dependencies
    summary_table.add_row("", "")
    summary_table.add_row("Dependency Calls", str(agg.dep_call_count))
    summary_table.add_row(
        "Dep Time (avg/max)",
        f"{agg.avg_dep_time_ms:.1f} / {agg.max_dep_time_ms:.1f} ms",
    )

    if agg.top_dependencies:
        deps_str = ", ".join(
            f"{d['service']}({d['call_count']}x, {d['avg_duration_ms']:.0f}ms)"
            for d in agg.top_dependencies[:5]
        )
        summary_table.add_row("Top Dependencies", deps_str)

    console.print(Panel(summary_table, title=title, border_style="blue"))


def _print_request_table(requests: list) -> None:
    """Print the per-request results table."""
    table = Table(title="Per-Request Resource Usage", show_lines=True)

    table.add_column("Request ID", style="dim", max_width=28)
    table.add_column("Time", style="dim", max_width=19)
    table.add_column("Pod", style="cyan", max_width=30)
    table.add_column("Duration\n(ms)", justify="right", style="yellow")
    table.add_column("CPU\n(ms)", justify="right", style="red")
    table.add_column("Memory\n(MB)", justify="right", style="magenta")
    table.add_column("Net In\n(KB)", justify="right", style="green")
    table.add_column("Net Out\n(KB)", justify="right", style="green")
    table.add_column("BW\n(Mbps)", justify="right", style="blue")
    table.add_column("Deps", justify="right", style="white")
    table.add_column("Dep Time\n(ms)", justify="right", style="white")

    for req in requests:
        table.add_row(
            req.request_id,
            req.timestamp,
            req.pod,
            f"{req.duration_ms:.1f}",
            f"{req.cpu_ms:.1f}",
            f"{req.memory_mb:.2f}",
            f"{req.net_in_kb:.1f}",
            f"{req.net_out_kb:.1f}",
            f"{req.bandwidth_mbps:.3f}",
            str(req.dep_count),
            f"{req.dep_time_ms:.1f}",
        )

    console.print(table)


def generate_detail_report(db_path: str, request_id: str) -> None:
    """Show detailed breakdown for a single request."""
    storage = StorageBackend(db_path)
    storage.initialize()

    # Query the specific request
    cursor = storage._conn.execute(
        """SELECT request_id, timestamp, pod_name, namespace, service_name,
                  container_name, process_name, pid, tid,
                  duration_ms, cpu_time_ms, memory_rss_bytes,
                  bytes_received, bytes_sent, network_bandwidth_bps,
                  local_port, remote_ip, remote_port,
                  total_dependency_time_ms, dependencies_json
           FROM request_profiles WHERE request_id = ?""",
        (request_id,),
    )
    row = cursor.fetchone()

    if not row:
        console.print(f"[red]Request '{request_id}' not found.[/red]")
        storage.close()
        return

    # Header
    console.print(Panel(f"[bold]Request Detail: {row[0]}[/bold]", border_style="green"))

    # Basic info
    info_table = Table(show_header=False, box=None)
    info_table.add_column("Field", style="bold")
    info_table.add_column("Value")

    info_table.add_row("Timestamp", datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M:%S.%f"))
    info_table.add_row("Namespace", row[3] or "N/A")
    info_table.add_row("Service", row[4] or "N/A")
    info_table.add_row("Pod", row[2] or "N/A")
    info_table.add_row("Container", row[5] or "N/A")
    info_table.add_row("Process", f"{row[6]} (PID: {row[7]}, TID: {row[8]})")
    info_table.add_row("Connection", f":{row[15]} <- {row[16]}:{row[17]}")

    console.print(info_table)
    console.print()

    # Resource metrics
    metrics_table = Table(title="Resource Consumption")
    metrics_table.add_column("Metric", style="bold cyan")
    metrics_table.add_column("Value", justify="right")
    metrics_table.add_column("Details", style="dim")

    duration_ms = row[9]
    cpu_ms = row[10]
    cpu_pct = (cpu_ms / duration_ms * 100) if duration_ms > 0 else 0

    metrics_table.add_row("Duration", f"{duration_ms:.2f} ms", "Wall clock time")
    metrics_table.add_row("CPU Time", f"{cpu_ms:.2f} ms", f"{cpu_pct:.1f}% utilization")
    metrics_table.add_row("Memory (RSS)", f"{row[11] / 1024 / 1024:.2f} MB", "Resident set size")
    metrics_table.add_row("Network In", f"{row[12] / 1024:.2f} KB", "Bytes received")
    metrics_table.add_row("Network Out", f"{row[13] / 1024:.2f} KB", "Bytes sent")
    metrics_table.add_row("Bandwidth", f"{row[14] / 1_000_000:.4f} Mbps", "Average throughput")

    console.print(metrics_table)
    console.print()

    # Dependencies
    deps = json.loads(row[19]) if row[19] else []
    if deps:
        dep_table = Table(title=f"Dependency Calls ({len(deps)} total, {row[18]:.1f}ms total)")
        dep_table.add_column("#", style="dim")
        dep_table.add_column("Service", style="cyan")
        dep_table.add_column("Destination")
        dep_table.add_column("Duration (ms)", justify="right", style="yellow")
        dep_table.add_column("Sent (KB)", justify="right")
        dep_table.add_column("Recv (KB)", justify="right")

        for i, dep in enumerate(deps, 1):
            dep_table.add_row(
                str(i),
                dep.get("service_name") or "unknown",
                f"{dep.get('dest_ip', '?')}:{dep.get('dest_port', '?')}",
                f"{dep.get('duration_ms', 0):.1f}",
                f"{dep.get('bytes_sent', 0) / 1024:.2f}",
                f"{dep.get('bytes_recv', 0) / 1024:.2f}",
            )

        console.print(dep_table)
    else:
        console.print("[dim]No dependency calls recorded for this request.[/dim]")

    storage.close()


def list_services(db_path: str, namespace: str | None = None) -> None:
    """List all monitored services with request counts."""
    storage = StorageBackend(db_path)
    storage.initialize()

    conditions = []
    params = []
    if namespace:
        conditions.append("namespace = ?")
        params.append(namespace)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    cursor = storage._conn.execute(
        f"""SELECT namespace, service_name, pod_name,
                   COUNT(*) as req_count,
                   AVG(duration_ms), AVG(cpu_time_ms),
                   AVG(memory_rss_bytes)
            FROM request_profiles
            {where}
            GROUP BY namespace, service_name, pod_name
            ORDER BY namespace, service_name, pod_name""",
        params,
    )

    table = Table(title="Monitored Services")
    table.add_column("Namespace", style="cyan")
    table.add_column("Service", style="bold")
    table.add_column("Pod", style="dim")
    table.add_column("Requests", justify="right")
    table.add_column("Avg Duration (ms)", justify="right", style="yellow")
    table.add_column("Avg CPU (ms)", justify="right", style="red")
    table.add_column("Avg Memory (MB)", justify="right", style="magenta")

    for row in cursor.fetchall():
        table.add_row(
            row[0] or "N/A",
            row[1] or "N/A",
            row[2] or "N/A",
            str(row[3]),
            f"{row[4]:.1f}",
            f"{row[5]:.1f}",
            f"{(row[6] or 0) / 1024 / 1024:.1f}",
        )

    console.print(table)
    storage.close()


def generate_alerts_report(
    db_path: str,
    namespace: str,
    service: str | None = None,
    since_minutes: int = 60,
    threshold_duration_ms: float | None = None,
    threshold_cpu_ms: float | None = None,
    threshold_memory_mb: float | None = None,
) -> None:
    """Show requests exceeding thresholds."""
    storage = StorageBackend(db_path)
    storage.initialize()

    requests = storage.query_requests(
        namespace=namespace, service=service, since_minutes=since_minutes, limit=500
    )

    flagged = []
    for req in requests:
        reasons = []
        if threshold_duration_ms and req.duration_ms > threshold_duration_ms:
            reasons.append(f"duration={req.duration_ms:.1f}ms > {threshold_duration_ms}ms")
        if threshold_cpu_ms and req.cpu_ms > threshold_cpu_ms:
            reasons.append(f"cpu={req.cpu_ms:.1f}ms > {threshold_cpu_ms}ms")
        if threshold_memory_mb and req.memory_mb > threshold_memory_mb:
            reasons.append(f"memory={req.memory_mb:.1f}MB > {threshold_memory_mb}MB")
        if reasons:
            flagged.append((req, reasons))

    if not flagged:
        console.print("[green]No requests exceeding thresholds.[/green]")
        storage.close()
        return

    table = Table(title=f"Alerts ({len(flagged)} requests exceeding thresholds)")
    table.add_column("Request ID", style="dim", max_width=28)
    table.add_column("Pod", style="cyan")
    table.add_column("Duration (ms)", justify="right", style="yellow")
    table.add_column("CPU (ms)", justify="right", style="red")
    table.add_column("Memory (MB)", justify="right", style="magenta")
    table.add_column("Reason", style="bold red")

    for req, reasons in flagged:
        table.add_row(
            req.request_id,
            req.pod,
            f"{req.duration_ms:.1f}",
            f"{req.cpu_ms:.1f}",
            f"{req.memory_mb:.2f}",
            "; ".join(reasons),
        )

    console.print(table)
    storage.close()
