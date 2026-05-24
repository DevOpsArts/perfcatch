"""
CLI main entry point - perfcatch command-line tool.
"""

from __future__ import annotations

import click

from .report import generate_report, generate_detail_report, list_services


@click.group()
@click.option(
    "--db",
    default="/var/lib/perfcatch/data.db",
    envvar="PERFCATCH_DB_PATH",
    help="Path to perfcatch database",
)
@click.pass_context
def cli(ctx: click.Context, db: str) -> None:
    """perfcatch - Per-request resource measurement for Kubernetes pods."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db


@cli.command()
@click.option("-n", "--namespace", help="Filter by Kubernetes namespace")
@click.option("-s", "--service", help="Filter by service/application name")
@click.option("-p", "--pod", help="Filter by pod name")
@click.option(
    "--since",
    default=60,
    type=int,
    help="Show data from last N minutes (default: 60)",
)
@click.option(
    "--limit",
    default=50,
    type=int,
    help="Maximum number of requests to show",
)
@click.option(
    "--sort",
    type=click.Choice(["duration", "cpu", "memory", "network", "deps"]),
    default="duration",
    help="Sort results by metric",
)
@click.pass_context
def report(
    ctx: click.Context,
    namespace: str | None,
    service: str | None,
    pod: str | None,
    since: int,
    limit: int,
    sort: str,
) -> None:
    """Generate aggregated report for a namespace/service."""
    generate_report(
        db_path=ctx.obj["db_path"],
        namespace=namespace,
        service=service,
        pod=pod,
        since_minutes=since,
        limit=limit,
        sort_by=sort,
    )


@cli.command()
@click.argument("request_id")
@click.pass_context
def detail(ctx: click.Context, request_id: str) -> None:
    """Show detailed breakdown for a single request."""
    generate_detail_report(db_path=ctx.obj["db_path"], request_id=request_id)


@cli.command()
@click.option("-n", "--namespace", help="Filter by namespace")
@click.pass_context
def services(ctx: click.Context, namespace: str | None) -> None:
    """List monitored services and their request counts."""
    list_services(db_path=ctx.obj["db_path"], namespace=namespace)


@cli.command()
@click.option("-n", "--namespace", required=True, help="Target namespace")
@click.option("-s", "--service", help="Target service name")
@click.option("--since", default=60, type=int, help="Time window in minutes")
@click.option(
    "--threshold-duration",
    type=float,
    help="Flag requests slower than N ms",
)
@click.option(
    "--threshold-cpu",
    type=float,
    help="Flag requests using more than N ms CPU",
)
@click.option(
    "--threshold-memory",
    type=float,
    help="Flag requests using more than N MB memory",
)
@click.pass_context
def alerts(
    ctx: click.Context,
    namespace: str,
    service: str | None,
    since: int,
    threshold_duration: float | None,
    threshold_cpu: float | None,
    threshold_memory: float | None,
) -> None:
    """Show requests exceeding resource thresholds."""
    from .report import generate_alerts_report

    generate_alerts_report(
        db_path=ctx.obj["db_path"],
        namespace=namespace,
        service=service,
        since_minutes=since,
        threshold_duration_ms=threshold_duration,
        threshold_cpu_ms=threshold_cpu,
        threshold_memory_mb=threshold_memory,
    )


if __name__ == "__main__":
    cli()
