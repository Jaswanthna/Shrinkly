"""In-memory MVP state for uploads and background jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.settings import Settings
from app.models import AnalysisResult
from app.models import PageAnalysis

if TYPE_CHECKING:
    from redis import Redis
    from rq import Queue


@dataclass
class UploadSession:
    """Upload metadata and filesystem locations."""

    upload_id: str
    filename: str
    expected_size: int
    directory: Path
    source_path: Path
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
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
    download_filename: str | None = None
    download_media_type: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    cancel_requested: bool = False


class RuntimeState:
    """Coordinates temporary upload data and in-process job state."""

    def __init__(
        self, settings: Settings, redis_client: Redis | None = None
    ) -> None:
        """Initialize runtime storage and synchronization."""
        self.settings = settings
        self.uploads: dict[str, UploadSession] = {}
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self._upload_starts: dict[str, deque[float]] = defaultdict(deque)
        self.redis: Redis | None = redis_client
        self.queue: Queue | None = None
        if self.redis is not None or settings.redis_url:
            from redis import Redis
            from rq import Queue

            if self.redis is None and settings.redis_url is not None:
                self.redis = Redis.from_url(settings.redis_url)
            self.queue = Queue("shrinkly", connection=self.redis)

    def save_upload(self, session: UploadSession) -> None:
        """Persist upload metadata when shared Redis state is enabled."""
        session.last_activity_at = time.time()
        with self.lock:
            self.uploads[session.upload_id] = session
        if self.redis is not None:
            self.redis.set(
                self._upload_key(session.upload_id),
                json.dumps(
                    {
                        "upload_id": session.upload_id,
                        "filename": session.filename,
                        "expected_size": session.expected_size,
                        "directory": str(session.directory),
                        "source_path": str(session.source_path),
                        "created_at": session.created_at,
                        "last_activity_at": session.last_activity_at,
                        "received_bytes": session.received_bytes,
                        "next_chunk": session.next_chunk,
                        "job_id": session.job_id,
                    }
                ),
                ex=self._pending_ttl,
            )

    @asynccontextmanager
    async def upload_guard(self, upload_id: str):
        """Serialize chunk and submit operations across API processes."""
        session = self.get_upload(upload_id)
        if session is None:
            yield None
            return
        if self.redis is None:
            async with session.lock:
                yield session
            return

        redis_lock = self.redis.lock(
            f"shrinkly:upload-lock:{upload_id}",
            timeout=self.settings.job_timeout_seconds + 300,
            blocking_timeout=30,
        )
        acquired = await asyncio.to_thread(redis_lock.acquire)
        if not acquired:
            raise TimeoutError("The upload is currently being updated.")
        try:
            yield self.get_upload(upload_id)
        finally:
            await asyncio.to_thread(redis_lock.release)

    def get_upload(self, upload_id: str) -> UploadSession | None:
        """Load an upload session from Redis or the in-process store."""
        if self.redis is not None:
            value = self.redis.get(self._upload_key(upload_id))
            if value is None:
                return None
            data = json.loads(value)
            with self.lock:
                existing = self.uploads.get(upload_id)
                session = UploadSession(
                    upload_id=data["upload_id"],
                    filename=data["filename"],
                    expected_size=data["expected_size"],
                    directory=Path(data["directory"]),
                    source_path=Path(data["source_path"]),
                    created_at=data["created_at"],
                    last_activity_at=data.get(
                        "last_activity_at", data["created_at"]
                    ),
                    received_bytes=data["received_bytes"],
                    next_chunk=data["next_chunk"],
                    job_id=data["job_id"],
                    lock=existing.lock if existing else asyncio.Lock(),
                )
                self.uploads[upload_id] = session
                return session
        with self.lock:
            return self.uploads.get(upload_id)

    def all_uploads(self) -> list[UploadSession]:
        """Return persisted upload metadata for expiry cleanup."""
        if self.redis is None:
            with self.lock:
                return list(self.uploads.values())
        sessions = []
        for key in self.redis.scan_iter(match="shrinkly:upload:*"):
            if isinstance(key, bytes):
                key = key.decode("ascii")
            upload_id = key.removeprefix("shrinkly:upload:")
            session = self.get_upload(upload_id)
            if session is not None:
                sessions.append(session)
        return sessions

    def remove_upload(self, upload_id: str) -> None:
        """Remove upload metadata from local and shared stores."""
        with self.lock:
            self.uploads.pop(upload_id, None)
        if self.redis is not None:
            self.redis.delete(self._upload_key(upload_id))

    def save_job(self, job: Job) -> None:
        """Persist job status and analysis in Redis when configured."""
        if self.redis is not None and self.redis.exists(self._cancel_key(job.job_id)):
            job.cancel_requested = True
            if job.status not in {"completed", "failed"}:
                job.status = "deleting"
        with self.lock:
            self.jobs[job.job_id] = job
        if self.redis is not None:
            analysis = None
            if job.analysis is not None:
                analysis = {
                    "format": job.analysis.format,
                    "page_count": job.analysis.page_count,
                    "file_size_bytes": job.analysis.file_size_bytes,
                    "estimated_tokens": job.analysis.estimated_tokens,
                    "pages": [
                        {
                            "page_number": page.page_number,
                            "scanned": page.scanned,
                            "text_characters": page.text_characters,
                        }
                        for page in job.analysis.pages
                    ],
                    "warnings": job.analysis.warnings,
                }
            data = {
                "job_id": job.job_id,
                "upload_id": job.upload_id,
                "status": job.status,
                "progress": job.progress,
                "format": job.format,
                "warnings": job.warnings,
                "analysis": analysis,
                "download_path": str(job.download_path) if job.download_path else None,
                "download_filename": job.download_filename,
                "download_media_type": job.download_media_type,
                "error": job.error,
                "created_at": job.created_at,
                "completed_at": job.completed_at,
                "cancel_requested": job.cancel_requested,
            }
            ttl = (
                self.settings.expiry_seconds
                if job.completed_at is not None
                else self._pending_ttl
            )
            self.redis.set(self._job_key(job.job_id), json.dumps(data), ex=ttl)

    def get_job(self, job_id: str) -> Job | None:
        """Load job state from Redis or the in-process store."""
        if self.redis is not None:
            value = self.redis.get(self._job_key(job_id))
            if value is None:
                return None
            data = json.loads(value)
            cancelled = bool(self.redis.exists(self._cancel_key(job_id)))
            analysis_data = data["analysis"]
            analysis = None
            if analysis_data is not None:
                analysis = AnalysisResult(
                    format=analysis_data["format"],
                    page_count=analysis_data["page_count"],
                    file_size_bytes=analysis_data["file_size_bytes"],
                    estimated_tokens=analysis_data["estimated_tokens"],
                    pages=[
                        PageAnalysis(**page) for page in analysis_data["pages"]
                    ],
                    warnings=analysis_data["warnings"],
                )
            job = Job(
                job_id=data["job_id"],
                upload_id=data["upload_id"],
                status=(
                    "deleting"
                    if cancelled and data["status"] not in {"completed", "failed"}
                    else data["status"]
                ),
                progress=data["progress"],
                format=data["format"],
                warnings=data["warnings"],
                analysis=analysis,
                download_path=(
                    Path(data["download_path"])
                    if data["download_path"] is not None
                    else None
                ),
                download_filename=data["download_filename"],
                download_media_type=data["download_media_type"],
                error=data["error"],
                created_at=data["created_at"],
                completed_at=data["completed_at"],
                cancel_requested=cancelled or data["cancel_requested"],
            )
            with self.lock:
                self.jobs[job_id] = job
            return job
        with self.lock:
            return self.jobs.get(job_id)

    def remove_job(self, job_id: str) -> None:
        """Remove job status from local and shared stores."""
        with self.lock:
            self.jobs.pop(job_id, None)
        if self.redis is not None:
            self.redis.delete(self._job_key(job_id), self._cancel_key(job_id))

    def request_job_cancellation(self, job: Job) -> None:
        """Persist a cancellation marker that worker updates cannot overwrite."""
        job.cancel_requested = True
        job.status = "deleting"
        if self.redis is not None:
            self.redis.set(self._cancel_key(job.job_id), "1", ex=self._pending_ttl)
        self.save_job(job)

    def enqueue_job(
        self,
        job_id: str,
        mode: str,
        token_limit: int,
        ocr_language: str,
    ) -> None:
        """Queue a bounded worker job, or leave execution to API background tasks."""
        if self.queue is None:
            return
        from rq.job import Callback

        self.queue.enqueue_call(
            "app.services.pipeline.process_queued_job",
            args=(job_id, mode, token_limit, ocr_language),
            job_id=job_id,
            timeout=self.settings.job_timeout_seconds,
            result_ttl=self.settings.expiry_seconds,
            failure_ttl=self.settings.expiry_seconds,
            on_failure=Callback("app.services.pipeline.mark_queued_job_failed"),
        )

    def allow_upload(self, client_ip: str, now: float | None = None) -> bool:
        """Apply a fixed-window upload-init limit per client address."""
        current_time = time.time() if now is None else now
        if self.redis is not None:
            digest = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
            key = f"shrinkly:rate:{digest}"
            pipeline = self.redis.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, 60 * 60, nx=True)
            count, _ = pipeline.execute()
            return count <= self.settings.uploads_per_ip_per_hour
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

    @property
    def _pending_ttl(self) -> int:
        """Keep queued work discoverable longer than the per-job execution bound."""
        return max(
            self.settings.expiry_seconds * 2,
            self.settings.job_timeout_seconds + 300,
        )

    @staticmethod
    def _upload_key(upload_id: str) -> str:
        return f"shrinkly:upload:{upload_id}"

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"shrinkly:job:{job_id}"

    @staticmethod
    def _cancel_key(job_id: str) -> str:
        return f"shrinkly:cancel:{job_id}"
