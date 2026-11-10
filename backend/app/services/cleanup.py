"""Expiry cleanup for upload and job working directories."""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path

from app.state import RuntimeState


logger = logging.getLogger("shrinkly.cleanup")


def cleanup_expired(runtime: RuntimeState, now: float | None = None) -> None:
    """Delete expired, inactive upload and job directories."""
    current_time = time.time() if now is None else now
    with runtime.lock:
        expired_ids: list[str] = []
        sessions = runtime.all_uploads()
        for session in sessions:
            upload_id = session.upload_id
            job = runtime.get_job(session.job_id) if session.job_id else None
            if job is not None and job.status in {"queued", "processing", "deleting"}:
                continue
            if session.lock.locked():
                continue
            if runtime.redis is not None and runtime.redis.exists(
                f"shrinkly:upload-lock:{upload_id}"
            ):
                continue
            expiry_time = (
                job.completed_at + runtime.settings.expiry_seconds
                if job is not None and job.completed_at is not None
                else session.last_activity_at + runtime.settings.expiry_seconds
            )
            if current_time >= expiry_time:
                expired_ids.append(upload_id)

        for upload_id in expired_ids:
            session = runtime.get_upload(upload_id)
            if session is None:
                continue
            if session.directory.parent == runtime.settings.data_dir:
                try:
                    shutil.rmtree(session.directory, ignore_errors=False)
                except OSError as exc:
                    logger.error("Expired-file cleanup failed (%s).", type(exc).__name__)
                    continue
            runtime.remove_upload(upload_id)
            if session.job_id:
                runtime.remove_job(session.job_id)

        known_directories = {
            session.directory.resolve() for session in runtime.all_uploads()
        }
        try:
            entries = tuple(runtime.settings.data_dir.iterdir())
        except OSError as exc:
            logger.error("Temporary-storage scan failed (%s).", type(exc).__name__)
            return
        for directory in entries:
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                uuid.UUID(directory.name)
                if directory.resolve() in known_directories:
                    continue
                if current_time - directory.stat().st_mtime < runtime.settings.expiry_seconds:
                    continue
                shutil.rmtree(directory, ignore_errors=False)
            except ValueError:
                continue
            except OSError as exc:
                logger.error("Orphaned-file cleanup failed (%s).", type(exc).__name__)
