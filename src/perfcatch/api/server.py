"""
Lightweight HTTP API server for perfcatch metrics.

Exposes:
  GET /metrics         - Prometheus-format metrics (for Grafana)
  GET /api/requests    - JSON per-request profiles
  GET /api/summary     - JSON aggregated summary
  GET /healthz         - Health check
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from ..store.ringbuffer import RingBufferStore, StoredRequest

logger = logging.getLogger(__name__)

# Histogram bucket boundaries for duration (ms)
_DURATION_BUCKETS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, float("inf"))
# Histogram bucket boundaries for CPU time (ms)
_CPU_BUCKETS = (0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, float("inf"))


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP request handler for metrics API."""

    # Set by the server factory
    store: RingBufferStore | None = None

    def log_message(self, format, *args):
        """Suppress default access logging."""
        pass

    def _send_json(self, data: dict | list, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = "text/plain", status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        routes = {
            "/healthz": self._handle_healthz,
            "/metrics": self._handle_prometheus,
            "/api/requests": self._handle_requests,
            "/api/summary": self._handle_summary,
        }

        handler = routes.get(path)
        if handler:
            try:
                handler(params)
            except Exception as e:
                logger.exception("API error on %s", path)
                self._send_json({"error": str(e)}, status=500)
        else:
            self._send_json({"error": "not found", "endpoints": list(routes.keys())}, status=404)

    def _handle_healthz(self, params: dict) -> None:
        store = self.store
        self._send_json({
            "status": "ok",
            "buffer_size": store.size if store else 0,
            "buffer_capacity": store.maxlen if store else 0,
        })

    def _handle_requests(self, params: dict) -> None:
        """Return per-request profiles as JSON."""
        store = self.store
        if not store:
            self._send_json({"error": "store not initialized"}, status=503)
            return

        limit = int(params.get("limit", [50])[0])
        process = params.get("process", [None])[0]
        port_str = params.get("port", [None])[0]
        port = int(port_str) if port_str else None
        since_min = int(params.get("since", [5])[0])
        namespace = params.get("namespace", [None])[0]
        pod = params.get("pod", [None])[0]
        correlation_id = params.get("correlation_id", [None])[0]

        entries = store.query(
            namespace=namespace,
            process=process,
            pod=pod,
            port=port,
            correlation_id=correlation_id,
            since_seconds=since_min * 60,
            limit=limit,
        )

        results = [
            {
                "request_id": e.request_id,
                "timestamp": e.timestamp,
                "pid": e.pid,
                "process_name": e.process_name,
                "pod_name": e.pod_name,
                "namespace": e.namespace,
                "service_name": e.service_name,
                "local_port": e.local_port,
                "remote_ip": e.remote_ip,
                "duration_ms": e.duration_ms,
                "cpu_time_ms": e.cpu_time_ms,
                "memory_rss_bytes": e.memory_rss_bytes,
                "bytes_received": e.bytes_received,
                "bytes_sent": e.bytes_sent,
                "correlation_id": e.correlation_id,
                "http_method": e.http_method,
                "http_path": e.http_path,
            }
            for e in entries
        ]
        self._send_json({"count": len(results), "requests": results})

    def _handle_summary(self, params: dict) -> None:
        """Return aggregated summary by process/port."""
        store = self.store
        if not store:
            self._send_json({"error": "store not initialized"}, status=503)
            return

        since_min = int(params.get("since", [5])[0])
        aggs = store.get_aggregates(since_seconds=since_min * 60)

        results = []
        for (ns, pod, proc, port), a in aggs.items():
            count = a["count"]
            results.append({
                "namespace": ns,
                "pod_name": pod,
                "process_name": proc,
                "local_port": port,
                "request_count": count,
                "avg_duration_ms": round(a["sum_duration_ms"] / count, 2) if count else 0,
                "max_duration_ms": round(a["max_duration_ms"], 2),
                "avg_cpu_ms": round(a["sum_cpu_ms"] / count, 2) if count else 0,
                "avg_rss_bytes": round(a["sum_rss"] / count) if count else 0,
                "total_bytes_rx": a["total_bytes_rx"],
                "total_bytes_tx": a["total_bytes_tx"],
            })

        results.sort(key=lambda x: x["request_count"], reverse=True)
        self._send_json({"since_minutes": since_min, "services": results})

    def _handle_prometheus(self, params: dict) -> None:
        """Return Prometheus-format metrics with histograms + per-request gauges."""
        store = self.store
        if not store:
            self._send_text("# no data\n", content_type="text/plain; version=0.0.4")
            return

        lines = []

        # --- Aggregate metrics (bounded cardinality) ---
        aggs = store.get_aggregates(since_seconds=300)

        def make_labels(ns, pod, proc, port):
            return f'namespace="{ns}",pod="{pod}",process="{proc}",port="{port}"'

        # Request count
        lines += [
            "# HELP perfcatch_requests_total Total number of requests observed",
            "# TYPE perfcatch_requests_total counter",
        ]
        for (ns, pod, proc, port), a in aggs.items():
            labels = make_labels(ns, pod, proc, port)
            lines.append(f"perfcatch_requests_total{{{labels}}} {a['count']}")

        # Duration histogram
        lines += [
            "# HELP perfcatch_request_duration_ms Request duration in milliseconds",
            "# TYPE perfcatch_request_duration_ms histogram",
        ]
        for (ns, pod, proc, port), a in aggs.items():
            labels = make_labels(ns, pod, proc, port)
            durations = sorted(a["durations"])
            count = len(durations)
            # Compute bucket counts
            for le in _DURATION_BUCKETS:
                le_label = "+Inf" if le == float("inf") else f"{le}"
                bucket_count = sum(1 for d in durations if d <= le)
                lines.append(
                    f'perfcatch_request_duration_ms_bucket{{{labels},le="{le_label}"}} {bucket_count}'
                )
            lines.append(f"perfcatch_request_duration_ms_sum{{{labels}}} {a['sum_duration_ms']:.2f}")
            lines.append(f"perfcatch_request_duration_ms_count{{{labels}}} {count}")

        # Max duration (still useful as gauge)
        lines += [
            "# HELP perfcatch_request_duration_ms_max Max request duration in ms",
            "# TYPE perfcatch_request_duration_ms_max gauge",
        ]
        for (ns, pod, proc, port), a in aggs.items():
            labels = make_labels(ns, pod, proc, port)
            lines.append(f"perfcatch_request_duration_ms_max{{{labels}}} {a['max_duration_ms']:.2f}")

        # CPU time histogram
        lines += [
            "# HELP perfcatch_request_cpu_ms CPU time per request in milliseconds",
            "# TYPE perfcatch_request_cpu_ms histogram",
        ]
        # We need per-request CPU values for histograms, get from recent buffer
        recent = store.query(since_seconds=300, limit=10000)
        # Group CPU times by service key
        cpu_by_key: dict[tuple, list[float]] = {}
        for r in recent:
            key = (r.namespace or "unknown", r.pod_name or "unknown", r.process_name or "unknown", r.local_port or 0)
            cpu_by_key.setdefault(key, []).append(r.cpu_time_ms)

        for (ns, pod, proc, port), cpu_values in cpu_by_key.items():
            labels = make_labels(ns, pod, proc, port)
            cpu_sorted = sorted(cpu_values)
            cpu_sum = sum(cpu_values)
            count = len(cpu_values)
            for le in _CPU_BUCKETS:
                le_label = "+Inf" if le == float("inf") else f"{le}"
                bucket_count = sum(1 for c in cpu_sorted if c <= le)
                lines.append(
                    f'perfcatch_request_cpu_ms_bucket{{{labels},le="{le_label}"}} {bucket_count}'
                )
            lines.append(f"perfcatch_request_cpu_ms_sum{{{labels}}} {cpu_sum:.2f}")
            lines.append(f"perfcatch_request_cpu_ms_count{{{labels}}} {count}")

        # Memory gauge (average)
        lines += [
            "# HELP perfcatch_memory_rss_bytes Average RSS bytes per process",
            "# TYPE perfcatch_memory_rss_bytes gauge",
        ]
        for (ns, pod, proc, port), a in aggs.items():
            labels = make_labels(ns, pod, proc, port)
            avg_rss = int(a["sum_rss"] / a["count"]) if a["count"] else 0
            lines.append(f"perfcatch_memory_rss_bytes{{{labels}}} {avg_rss}")

        # Network counters
        lines += [
            "# HELP perfcatch_network_rx_bytes_total Total bytes received",
            "# TYPE perfcatch_network_rx_bytes_total counter",
        ]
        for (ns, pod, proc, port), a in aggs.items():
            labels = make_labels(ns, pod, proc, port)
            lines.append(f"perfcatch_network_rx_bytes_total{{{labels}}} {a['total_bytes_rx']}")

        lines += [
            "# HELP perfcatch_network_tx_bytes_total Total bytes sent",
            "# TYPE perfcatch_network_tx_bytes_total counter",
        ]
        for (ns, pod, proc, port), a in aggs.items():
            labels = make_labels(ns, pod, proc, port)
            lines.append(f"perfcatch_network_tx_bytes_total{{{labels}}} {a['total_bytes_tx']}")

        # --- Per-request individual metrics (for Grafana table) ---
        # Correlated requests (3hr lookback) + recent uncorrelated (5min)
        correlated_rows = store.get_correlated(since_seconds=10800, limit=200)
        uncorrelated_rows = store.get_uncorrelated_recent(since_seconds=300, limit=50)

        seen_ids = set(r.request_id for r in correlated_rows)
        individual_rows = correlated_rows + [r for r in uncorrelated_rows if r.request_id not in seen_ids]

        # Filter out system namespaces
        individual_rows = [
            r for r in individual_rows
            if r.namespace and r.namespace not in ("kube-system", "monitoring", "unknown", "")
        ]

        if individual_rows:
            def req_labels(r: StoredRequest) -> str:
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

            lines += [
                "# HELP perfcatch_req_duration_ms Per-request duration in ms",
                "# TYPE perfcatch_req_duration_ms gauge",
            ]
            for r in individual_rows:
                lines.append(f"perfcatch_req_duration_ms{{{req_labels(r)}}} {r.duration_ms:.2f}")

            lines += [
                "# HELP perfcatch_req_cpu_ms Per-request CPU time in ms",
                "# TYPE perfcatch_req_cpu_ms gauge",
            ]
            for r in individual_rows:
                lines.append(f"perfcatch_req_cpu_ms{{{req_labels(r)}}} {r.cpu_time_ms:.2f}")

            lines += [
                "# HELP perfcatch_req_memory_bytes Per-request memory RSS",
                "# TYPE perfcatch_req_memory_bytes gauge",
            ]
            for r in individual_rows:
                lines.append(f"perfcatch_req_memory_bytes{{{req_labels(r)}}} {int(r.memory_rss_bytes or 0)}")

            lines += [
                "# HELP perfcatch_req_bytes_rx Per-request bytes received",
                "# TYPE perfcatch_req_bytes_rx gauge",
            ]
            for r in individual_rows:
                lines.append(f"perfcatch_req_bytes_rx{{{req_labels(r)}}} {r.bytes_received}")

            lines += [
                "# HELP perfcatch_req_bytes_tx Per-request bytes sent",
                "# TYPE perfcatch_req_bytes_tx gauge",
            ]
            for r in individual_rows:
                lines.append(f"perfcatch_req_bytes_tx{{{req_labels(r)}}} {r.bytes_sent}")

            lines += [
                "# HELP perfcatch_req_start_time Request start time as unix epoch milliseconds",
                "# TYPE perfcatch_req_start_time gauge",
            ]
            for r in individual_rows:
                lines.append(f"perfcatch_req_start_time{{{req_labels(r)}}} {r.timestamp * 1000:.3f}")

            lines += [
                "# HELP perfcatch_req_end_time Request completion time as unix epoch milliseconds",
                "# TYPE perfcatch_req_end_time gauge",
            ]
            for r in individual_rows:
                end_ms = (r.timestamp * 1000) + r.duration_ms
                lines.append(f"perfcatch_req_end_time{{{req_labels(r)}}} {end_ms:.3f}")

        # --- Buffer stats ---
        lines += [
            "# HELP perfcatch_buffer_size Current entries in ring buffer",
            "# TYPE perfcatch_buffer_size gauge",
            f"perfcatch_buffer_size {store.size}",
            "# HELP perfcatch_buffer_capacity Max ring buffer capacity",
            "# TYPE perfcatch_buffer_capacity gauge",
            f"perfcatch_buffer_capacity {store.maxlen}",
        ]

        self._send_text("\n".join(lines) + "\n", content_type="text/plain; version=0.0.4")


def start_api_server(port: int = 9090, store: RingBufferStore | None = None) -> HTTPServer:
    """Start the metrics API server in a background thread."""
    MetricsHandler.store = store

    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="api-server")
    thread.start()
    logger.info("Metrics API server started on port %d", port)
    return server
