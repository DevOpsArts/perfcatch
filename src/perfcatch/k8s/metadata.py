"""
Kubernetes metadata resolver - maps PIDs and IPs to pod/service info.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# SA token and CA paths for in-cluster auth
_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


@dataclass
class PodInfo:
    """Kubernetes pod metadata."""

    pod_name: str
    namespace: str
    container_name: str
    service_name: str
    node_name: str
    pod_ip: str
    container_pids: list[int]


class PodMetadataResolver:
    """Resolves PIDs and IPs to Kubernetes pod/service metadata."""

    def __init__(self, refresh_interval: float = 30.0):
        self._refresh_interval = refresh_interval
        self._pid_to_pod: dict[int, PodInfo] = {}
        self._ip_to_service: dict[str, str] = {}
        self._comm_to_pod: dict[str, PodInfo] = {}
        self._port_to_pod: dict[int, PodInfo] = {}
        self._node_name = os.environ.get("NODE_NAME", "")
        self._lock = threading.Lock()
        self._running = False
        self._refresh_thread: threading.Thread | None = None

        # Build K8s API base URL
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "10.96.0.1")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self._api_base = f"https://{host}:{port}"

        # Set up SSL context
        self._ssl_ctx = ssl.create_default_context()
        if os.path.exists(_CA_PATH):
            self._ssl_ctx.load_verify_locations(_CA_PATH)

    def _get_token(self) -> str:
        """Read the current SA token (may be rotated)."""
        with open(_TOKEN_PATH) as f:
            return f.read().strip()

    def _k8s_get(self, path: str) -> dict:
        """Make an authenticated GET request to the K8s API."""
        url = f"{self._api_base}{path}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._get_token()}"}
        )
        resp = urllib.request.urlopen(req, context=self._ssl_ctx, timeout=10)
        return json.loads(resp.read())

    def start(self) -> None:
        """Start background metadata refresh."""
        self._running = True
        self._refresh_metadata()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop, daemon=True
        )
        self._refresh_thread.start()

    def stop(self) -> None:
        """Stop background refresh."""
        self._running = False

    def _refresh_loop(self) -> None:
        """Periodically refresh pod metadata."""
        while self._running:
            time.sleep(self._refresh_interval)
            try:
                self._refresh_metadata()
            except Exception as e:
                logger.warning("Failed to refresh metadata: %s", e)

    def _refresh_metadata(self) -> None:
        """Fetch current pod and service metadata from K8s API."""
        try:
            # Get pods on this node
            params = "?limit=500"
            if self._node_name:
                params += f"&fieldSelector=spec.nodeName%3D{self._node_name}"
            data = self._k8s_get(f"/api/v1/pods{params}")

            pid_map: dict[int, PodInfo] = {}
            ip_map: dict[str, str] = {}
            comm_map: dict[str, PodInfo] = {}
            port_map: dict[int, PodInfo] = {}

            for pod in data.get("items", []):
                metadata = pod.get("metadata", {})
                status = pod.get("status", {})
                spec = pod.get("spec", {})
                pod_ip = status.get("podIP")
                if not pod_ip:
                    continue

                # Determine service name from labels
                labels = metadata.get("labels", {})
                service_name = (
                    labels.get("app.kubernetes.io/name")
                    or labels.get("app")
                    or labels.get("name")
                    or metadata.get("name", "")
                )

                container_statuses = status.get("containerStatuses", [])
                container_pids = []

                for cs in container_statuses:
                    cid = cs.get("containerID", "")
                    state = cs.get("state", {})
                    if cid and "running" in state:
                        container_pid = self._get_container_pid(cid)
                        if container_pid:
                            container_pids.append(container_pid)

                info = PodInfo(
                    pod_name=metadata.get("name", ""),
                    namespace=metadata.get("namespace", ""),
                    container_name=container_statuses[0].get("name", "") if container_statuses else "",
                    service_name=service_name,
                    node_name=spec.get("nodeName", ""),
                    pod_ip=pod_ip,
                    container_pids=container_pids,
                )

                for pid in container_pids:
                    pid_map[pid] = info
                    # Read comm name for this container PID
                    try:
                        with open(f"/proc/{pid}/comm") as f:
                            comm = f.read().strip()[:15]
                            comm_map[comm] = info
                    except (IOError, OSError):
                        pass

                # Map container ports to pod info
                for container in spec.get("containers", []):
                    for port_spec in container.get("ports", []):
                        cp = port_spec.get("containerPort")
                        if cp:
                            port_map[cp] = info

                ip_map[pod_ip] = service_name

            # Get ClusterIP services for dependency resolution
            svc_data = self._k8s_get("/api/v1/services")
            for svc in svc_data.get("items", []):
                svc_meta = svc.get("metadata", {})
                svc_spec = svc.get("spec", {})
                cluster_ip = svc_spec.get("clusterIP", "")
                if cluster_ip and cluster_ip != "None":
                    svc_name = f"{svc_meta.get('name')}.{svc_meta.get('namespace')}"
                    ip_map[cluster_ip] = svc_name

            with self._lock:
                self._pid_to_pod = pid_map
                self._ip_to_service = ip_map
                self._comm_to_pod = comm_map
                self._port_to_pod = port_map

            logger.info(
                "Metadata refresh: %d PIDs, %d comms, %d ports, %d IPs",
                len(pid_map), len(comm_map), len(port_map), len(ip_map),
            )

        except Exception as e:
            logger.error("Metadata refresh error: %s", e)

    def _get_container_pid(self, container_id: str) -> int | None:
        """Get the main PID of a container from /proc or CRI."""
        # Strip runtime prefix (e.g., "containerd://abc123")
        if "://" in container_id:
            cid = container_id.split("://")[1]
        else:
            cid = container_id

        # Try to find PID via /proc (requires hostPID)
        try:
            import glob

            for proc_dir in glob.glob("/proc/[0-9]*/cgroup"):
                try:
                    with open(proc_dir) as f:
                        content = f.read()
                        if cid[:12] in content:
                            pid = int(proc_dir.split("/")[2])
                            return pid
                except (IOError, ValueError):
                    continue
        except Exception:
            pass

        return None

    def resolve_pid(self, pid: int) -> PodInfo | None:
        """Look up pod info for a given PID (tries PID map first, then /proc)."""
        with self._lock:
            if pid in self._pid_to_pod:
                return self._pid_to_pod[pid]
            return None

    def resolve_comm(self, comm: str, local_port: int = 0) -> PodInfo | None:
        """Look up pod info by process comm name and optional port.

        This is the primary resolution method when eBPF PIDs don't match
        /proc PIDs (e.g., Docker-in-VM PID namespace mismatch).
        """
        with self._lock:
            # Try exact comm match
            if comm in self._comm_to_pod:
                return self._comm_to_pod[comm]

            # Try port match
            if local_port and local_port in self._port_to_pod:
                return self._port_to_pod[local_port]

            # Try prefix match (BPF comm is truncated to 15 chars)
            for known_comm, info in self._comm_to_pod.items():
                if known_comm.startswith(comm) or comm.startswith(known_comm):
                    return info

            return None

    @staticmethod
    def _same_pod_cgroup(cgroup1: str, cgroup2: str) -> bool:
        """Check if two cgroup entries belong to the same pod."""
        import re
        pod_pattern = re.compile(r'/pod[a-f0-9-]+')
        pods1 = pod_pattern.findall(cgroup1)
        pods2 = pod_pattern.findall(cgroup2)
        if pods1 and pods2:
            return pods1[0] == pods2[0]
        return False

    def resolve_ip(self, ip: str) -> str | None:
        """Look up service name for an IP address."""
        with self._lock:
            return self._ip_to_service.get(ip)

    def get_pods_on_node(
        self,
        namespace: str | None = None,
        pod_names: list[str] | None = None,
    ) -> list[PodInfo]:
        """Get pods running on this node, optionally filtered."""
        with self._lock:
            seen = set()
            results = []
            for info in self._pid_to_pod.values():
                if info.pod_name in seen:
                    continue
                if namespace and info.namespace != namespace:
                    continue
                if pod_names and info.pod_name not in pod_names:
                    continue
                seen.add(info.pod_name)
                results.append(info)
            return results
