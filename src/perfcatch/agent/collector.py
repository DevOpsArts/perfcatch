"""
eBPF event collector - loads BPF programs and collects events from kernel.

Reads perf events from eBPF maps, parses them into structured Python objects,
and forwards them to the correlator for request-level aggregation.
"""

from __future__ import annotations

import ctypes
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bcc import BPF

BPF_DIR = Path(__file__).parent / "bpf"


@dataclass
class RequestEvent:
    """A completed incoming request with resource measurements."""

    start_ns: int
    duration_ns: int
    cpu_time_ns: int
    mem_rss_bytes: int
    pid: int
    tid: int
    bytes_sent: int
    bytes_recv: int
    local_port: int
    remote_port: int
    src_ip: str
    dst_ip: str
    comm: str
    correlation_id: str = ""  # Extracted from HTTP headers if present
    http_method: str = ""     # HTTP method (GET, POST, etc.)
    http_path: str = ""       # Request path


@dataclass
class DependencyEvent:
    """An outgoing dependency call made during request processing."""

    start_ns: int
    duration_ns: int
    pid: int
    tid: int
    bytes_sent: int
    bytes_recv: int
    dest_ip: str
    dest_port: int
    protocol: int
    comm: str


@dataclass
class MemorySnapshot:
    """Memory usage snapshot for a process."""

    timestamp_ns: int
    pid: int
    rss_bytes: int
    vm_bytes: int
    comm: str


def _ip_to_str(ip_int: int) -> str:
    """Convert integer IP to dotted notation."""
    return f"{ip_int & 0xFF}.{(ip_int >> 8) & 0xFF}.{(ip_int >> 16) & 0xFF}.{(ip_int >> 24) & 0xFF}"


# Common correlation ID header names (case-insensitive matching)
_DEFAULT_CORRELATION_HEADERS = (
    "x-correlation-id",
    "x-request-id",
    "x-trace-id",
    "traceparent",
    "x-amzn-trace-id",
    "request-id",
    "correlation-id",
)

def _get_correlation_headers() -> tuple:
    """Build correlation headers list from defaults + PERFCATCH_CORRELATION_HEADERS env var."""
    import os
    extra = os.environ.get("PERFCATCH_CORRELATION_HEADERS", "")
    if not extra.strip():
        return _DEFAULT_CORRELATION_HEADERS
    custom = tuple(h.strip().lower() for h in extra.split(",") if h.strip())
    return custom + _DEFAULT_CORRELATION_HEADERS

_CORRELATION_HEADERS = _get_correlation_headers()


def _parse_http_headers(raw: bytes) -> tuple[str, str, str]:
    """Parse HTTP headers from raw bytes captured by eBPF.

    Returns (correlation_id, http_method, http_path).
    correlation_id is empty string if no known header found.
    """
    try:
        # Decode as ASCII, ignore errors (binary noise)
        text = raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
        if not text:
            return "", "", ""

        lines = text.split("\r\n")
        if not lines:
            return "", "", ""

        # Parse request line: "GET /path HTTP/1.1"
        http_method = ""
        http_path = ""
        request_line = lines[0]
        parts = request_line.split(" ")
        if len(parts) >= 2 and parts[0] in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            http_method = parts[0]
            http_path = parts[1].split("?")[0]  # Strip query params

        # Search for correlation headers
        correlation_id = ""
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            name_lower = name.strip().lower()
            if name_lower in _CORRELATION_HEADERS:
                correlation_id = value.strip()
                # For traceparent, extract trace-id (2nd field)
                if name_lower == "traceparent" and "-" in correlation_id:
                    parts = correlation_id.split("-")
                    if len(parts) >= 2:
                        correlation_id = parts[1]
                break

        return correlation_id, http_method, http_path
    except Exception:
        return "", "", ""


def _read_proc_rss(pid: int) -> int:
    """Read RSS in bytes from /proc/[pid]/status. Returns 0 on failure."""
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


# Cache: comm name -> (visible_pid, last_check_time)
_proc_cache: dict[str, tuple[int, float]] = {}


def _read_rss_by_comm(pid: int, comm: str) -> int:
    """Read RSS for a process, handling PID namespace mismatches.

    First tries the exact PID. If that fails, scans /proc for a process
    with matching comm name (handles Docker Desktop VM PID nesting).
    """
    import time as _time

    # Try exact PID first (works in standard deployments)
    rss = _read_proc_rss(pid)
    if rss > 0:
        return rss

    # Check cache (refresh every 30s)
    now = _time.time()
    cached = _proc_cache.get(comm)
    if cached and (now - cached[1]) < 30:
        rss = _read_proc_rss(cached[0])
        if rss > 0:
            return rss

    # Scan /proc for matching comm
    try:
        import os
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", "r") as f:
                    proc_comm = f.read().strip()
                if proc_comm == comm:
                    visible_pid = int(entry)
                    rss = _read_proc_rss(visible_pid)
                    if rss > 0:
                        _proc_cache[comm] = (visible_pid, now)
                        return rss
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return 0


class EventCollector:
    """Loads eBPF programs and collects kernel events."""

    def __init__(
        self,
        target_pids: set[int] | None = None,
        on_request: Callable[[RequestEvent], None] | None = None,
        on_dependency: Callable[[DependencyEvent], None] | None = None,
        on_memory: Callable[[MemorySnapshot], None] | None = None,
    ):
        self._target_pids = target_pids or set()
        self._on_request = on_request
        self._on_dependency = on_dependency
        self._on_memory = on_memory
        self._running = False
        self._bpf_request: BPF | None = None
        self._bpf_dep: BPF | None = None
        self._bpf_resource: BPF | None = None

    def load_programs(self) -> None:
        """Load and attach eBPF programs."""
        # Load request tracker
        request_src = (BPF_DIR / "request_tracker.c").read_text()
        self._bpf_request = BPF(text=request_src)
        self._bpf_request.attach_kretprobe(
            event="inet_csk_accept", fn_name="trace_accept_return"
        )
        self._bpf_request.attach_kprobe(
            event="tcp_sendmsg", fn_name="trace_tcp_sendmsg"
        )
        self._bpf_request.attach_kprobe(
            event="tcp_recvmsg", fn_name="trace_tcp_recvmsg_entry"
        )
        self._bpf_request.attach_kretprobe(
            event="tcp_recvmsg", fn_name="trace_tcp_recvmsg_return"
        )
        self._bpf_request.attach_kprobe(
            event="tcp_close", fn_name="trace_tcp_close"
        )

        # Load dependency tracker
        dep_src = (BPF_DIR / "dependency_tracker.c").read_text()
        self._bpf_dep = BPF(text=dep_src)
        self._bpf_dep.attach_kprobe(
            event="tcp_v4_connect", fn_name="trace_connect_entry"
        )
        self._bpf_dep.attach_kretprobe(
            event="tcp_v4_connect", fn_name="trace_connect_return"
        )
        self._bpf_dep.attach_kprobe(
            event="tcp_close", fn_name="trace_dep_close"
        )

        # Load resource tracker
        resource_src = (BPF_DIR / "resource_tracker.c").read_text()
        self._bpf_resource = BPF(text=resource_src)

        # Register perf event callbacks
        self._bpf_request["request_events"].open_perf_buffer(
            self._handle_request_event, page_cnt=256
        )
        self._bpf_dep["dep_events"].open_perf_buffer(
            self._handle_dep_event, page_cnt=128
        )
        self._bpf_resource["mem_events"].open_perf_buffer(
            self._handle_mem_event, page_cnt=64
        )

    def _handle_request_event(self, cpu: int, data: ctypes.Structure, size: int) -> None:
        """Process a completed request event from eBPF."""
        event = self._bpf_request["request_events"].event(data)

        # Filter by target PIDs if configured
        if self._target_pids and event.pid not in self._target_pids:
            return

        # Read RSS from /proc (reliable across kernels)
        comm = event.comm.decode("utf-8", errors="replace").rstrip("\x00")
        rss_bytes = _read_rss_by_comm(event.pid, comm)

        # Parse correlation ID and HTTP info from captured headers
        correlation_id = ""
        http_method = ""
        http_path = ""
        if event.header_len > 0:
            raw_headers = bytes(event.header_buf[:event.header_len])
            correlation_id, http_method, http_path = _parse_http_headers(raw_headers)

        req = RequestEvent(
            start_ns=event.start_ns,
            duration_ns=event.duration_ns,
            cpu_time_ns=event.cpu_time_ns,
            mem_rss_bytes=rss_bytes,
            pid=event.pid,
            tid=event.tid,
            bytes_sent=event.bytes_sent,
            bytes_recv=event.bytes_recv,
            local_port=event.lport,
            remote_port=event.dport,
            src_ip=_ip_to_str(event.saddr),
            dst_ip=_ip_to_str(event.daddr),
            comm=comm,
            correlation_id=correlation_id,
            http_method=http_method,
            http_path=http_path,
        )

        if self._on_request:
            self._on_request(req)

    def _handle_dep_event(self, cpu: int, data: ctypes.Structure, size: int) -> None:
        """Process a dependency call event from eBPF."""
        event = self._bpf_dep["dep_events"].event(data)

        if self._target_pids and event.pid not in self._target_pids:
            return

        dep = DependencyEvent(
            start_ns=event.start_ns,
            duration_ns=event.duration_ns,
            pid=event.pid,
            tid=event.tid,
            bytes_sent=event.bytes_sent,
            bytes_recv=event.bytes_recv,
            dest_ip=_ip_to_str(event.dest_ip),
            dest_port=event.dest_port,
            protocol=event.protocol,
            comm=event.comm.decode("utf-8", errors="replace").rstrip("\x00"),
        )

        if self._on_dependency:
            self._on_dependency(dep)

    def _handle_mem_event(self, cpu: int, data: ctypes.Structure, size: int) -> None:
        """Process a memory snapshot event."""
        event = self._bpf_resource["mem_events"].event(data)

        if self._target_pids and event.pid not in self._target_pids:
            return

        snap = MemorySnapshot(
            timestamp_ns=event.timestamp_ns,
            pid=event.pid,
            rss_bytes=event.rss_bytes,
            vm_bytes=event.vm_bytes,
            comm=event.comm.decode("utf-8", errors="replace").rstrip("\x00"),
        )

        if self._on_memory:
            self._on_memory(snap)

    def start(self) -> None:
        """Start polling for eBPF events."""
        self._running = True
        while self._running:
            if self._bpf_request:
                self._bpf_request.perf_buffer_poll(timeout=100)
            if self._bpf_dep:
                self._bpf_dep.perf_buffer_poll(timeout=100)
            if self._bpf_resource:
                self._bpf_resource.perf_buffer_poll(timeout=100)

    def stop(self) -> None:
        """Stop the event collection loop."""
        self._running = False

    def get_cpu_time(self, pid: int) -> int:
        """Read accumulated CPU time for a PID from the BPF map."""
        if not self._bpf_resource:
            return 0
        cpu_map = self._bpf_resource["pid_cpu_time"]
        key = ctypes.c_uint32(pid)
        try:
            val = cpu_map[key]
            return val.value
        except KeyError:
            return 0
