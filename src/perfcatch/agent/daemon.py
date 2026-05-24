"""
Agent daemon - main entry point for the eBPF data collection agent.

Runs as a DaemonSet pod on each Kubernetes node. Loads eBPF programs,
collects events, correlates them, and stores profiles in memory.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from .collector import EventCollector
from .correlator import RequestCorrelator
from ..api.server import start_api_server
from ..store.ringbuffer import RingBufferStore
from ..store.remote_write import RemoteWriteClient
from ..k8s.metadata import PodMetadataResolver

logger = logging.getLogger(__name__)


class AgentDaemon:
    """Main agent daemon that orchestrates collection and storage."""

    def __init__(
        self,
        target_namespace: str | None = None,
        target_pods: list[str] | None = None,
        buffer_size: int = 50000,
        flush_interval: float = 2.0,
        remote_write_url: str | None = None,
        db_path: str | None = None,
    ):
        self._target_namespace = target_namespace
        self._target_pods = target_pods or []
        self._buffer_size = buffer_size
        self._flush_interval = flush_interval
        self._remote_write_url = remote_write_url
        self._db_path = db_path
        self._running = False

        self._correlator = RequestCorrelator()
        self._collector = EventCollector(
            on_request=self._correlator.handle_request,
            on_dependency=self._correlator.handle_dependency,
            on_memory=self._correlator.handle_memory,
        )
        self._store = RingBufferStore(maxlen=buffer_size)
        self._metadata = PodMetadataResolver()
        self._remote_writer: RemoteWriteClient | None = None
        self._db: StorageBackend | None = None

    def _resolve_target_pids(self) -> set[int]:
        """Get PIDs for target pods on this node."""
        if not self._target_namespace:
            return set()  # Track all if no filter

        pids = set()
        pods = self._metadata.get_pods_on_node(
            namespace=self._target_namespace,
            pod_names=self._target_pods or None,
        )
        for pod in pods:
            for container_pid in pod.container_pids:
                pids.add(container_pid)
        return pids

    def _flush_loop(self) -> None:
        """Periodically flush completed profiles to ring buffer."""
        while self._running:
            time.sleep(self._flush_interval)
            self._correlator.cleanup_stale()

            profiles = self._correlator.drain_completed()
            if not profiles:
                continue

            # Enrich with K8s metadata
            for profile in profiles:
                meta = self._metadata.resolve_comm(
                    profile.process_name, profile.local_port
                )
                if not meta:
                    meta = self._metadata.resolve_pid(profile.pid)
                if meta:
                    profile.pod_name = meta.pod_name
                    profile.namespace = meta.namespace
                    profile.container_name = meta.container_name
                    profile.service_name = meta.service_name

                # Resolve dependency service names
                for dep in profile.dependencies:
                    svc = self._metadata.resolve_ip(dep.dest_ip)
                    if svc:
                        dep.service_name = svc

            # Store in ring buffer (zero I/O)
            self._store.store_profiles(profiles)

            # Persist to SQLite if configured (survives pod restart)
            if self._db:
                try:
                    self._db.store_profiles(profiles)
                except Exception as e:
                    logger.warning("SQLite persist error: %s", e)

            # Push to remote write if configured
            if self._remote_writer:
                from ..store.ringbuffer import StoredRequest
                # Get the entries just stored (they're the most recent)
                recent = self._store.query(since_seconds=self._flush_interval + 1, limit=len(profiles))
                self._remote_writer.enqueue_requests(recent)

            logger.debug("Flushed %d request profiles (buffer: %d/%d)",
                        len(profiles), self._store.size, self._store.maxlen)

    def start(self) -> None:
        """Start the agent daemon."""
        logger.info("Starting perfcatch agent daemon")
        self._running = True

        # Start metrics API server (uses ring buffer directly)
        api_port = int(os.environ.get("PERFCATCH_API_PORT", "9090"))
        start_api_server(port=api_port, store=self._store)

        # Initialize SQLite persistence if configured
        if self._db_path:
            from ..store.backend import StorageBackend
            self._db = StorageBackend(db_path=self._db_path)
            self._db.initialize()
            logger.info("SQLite persistence enabled → %s", self._db_path)

        # Start remote write client if configured
        if self._remote_write_url:
            self._remote_writer = RemoteWriteClient(endpoint=self._remote_write_url)
            self._remote_writer.start()
            logger.info("Remote write enabled → %s", self._remote_write_url)

        # Start K8s metadata resolver
        self._metadata.start()

        # Resolve target PIDs
        target_pids = self._resolve_target_pids()
        if target_pids:
            self._collector._target_pids = target_pids
            logger.info("Tracking %d PIDs", len(target_pids))
        else:
            logger.info("Tracking all PIDs (no namespace filter)")

        # Load eBPF programs
        self._collector.load_programs()
        logger.info("eBPF programs loaded successfully")

        # Start flush thread
        flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        flush_thread.start()

        # Handle signals
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())

        # Start event polling (blocks)
        try:
            self._collector.start()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the agent daemon gracefully."""
        logger.info("Stopping perfcatch agent daemon")
        self._running = False
        self._collector.stop()
        if self._remote_writer:
            self._remote_writer.stop()


def run_agent() -> None:
    """Entry point for the agent daemon."""
    log_level = os.environ.get("PERFCATCH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    namespace = os.environ.get("PERFCATCH_NAMESPACE")
    pods_csv = os.environ.get("PERFCATCH_PODS", "")
    pods = [p.strip() for p in pods_csv.split(",") if p.strip()] if pods_csv else []
    buffer_size = int(os.environ.get("PERFCATCH_BUFFER_SIZE", "50000"))
    flush_interval = float(os.environ.get("PERFCATCH_FLUSH_INTERVAL", "2"))
    remote_write_url = os.environ.get("PERFCATCH_REMOTE_WRITE_URL", "").strip() or None
    db_path = os.environ.get("PERFCATCH_DB_PATH", "").strip() or None

    daemon = AgentDaemon(
        target_namespace=namespace,
        target_pods=pods,
        buffer_size=buffer_size,
        flush_interval=flush_interval,
        remote_write_url=remote_write_url,
        db_path=db_path,
    )
    daemon.start()


if __name__ == "__main__":
    import traceback
    try:
        run_agent()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
