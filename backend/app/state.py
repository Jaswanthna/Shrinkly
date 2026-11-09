"""In-memory MVP state for uploads and background jobs."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from app.settings import Settings
from app.models import AnalysisResult


@dataclass
class UploadSession:
    """Upload metadata and filesystem locations."""

    upload_id: str
    filename: str
    expected_size: int
    directory: Path
    source_path: Path
    created_at: float = field(default_factory=time.time)
    received_bytes: int = 0
    next_chunk: int = 0
    job_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Job:
    """Processing status returned by the job API."""

    job_id: str
    upload_id: str
    status: str = "queued"
    progress: int = 0
    format: str = "pdf"
    warnings: list[str] = field(default_factory=list)
    analysis: AnalysisResult | None = None
    download_path: Path | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    cancel_requested: bool = False


class RuntimeState:
    """Coordinates temporary upload data and in-process job state."""

    def __init__(self, settings: Settings) -> None:
        """Initialize runtime storage and synchronization."""
        self.settings = settings
        self.uploads: dict[str, UploadSession] = {}
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self._upload_starts: dict[str, deque[float]] = defaultdict(deque)

    def allow_upload(self, client_ip: str, now: float | None = None) -> bool:
        """Apply a fixed-window upload-init limit per client address."""
        current_time = time.time() if now is None else now
        with self.lock:
            stale_clients = [
                ip
                for ip, starts in self._upload_starts.items()
                if not starts or starts[-1] <= current_time - 60 * 60
            ]
            for ip in stale_clients:
                self._upload_starts.pop(ip, None)
            starts = self._upload_starts[client_ip]
            cutoff = current_time - 60 * 60
            while starts and starts[0] <= cutoff:
                starts.popleft()
            if len(starts) >= self.settings.uploads_per_ip_per_hour:
                return False
            starts.append(current_time)
            return True
