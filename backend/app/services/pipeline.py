"""Background processing pipeline for uploaded files."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import zipfile
from types import TracebackType
from typing import TYPE_CHECKING, Type

from app.models import ProcessingOptions
from app.registry import get_handler
from app.services.errors import ShrinklyError
from app.services.package import build_package
from app.services.split import split_markdown
from app.state import Job, RuntimeState


logger = logging.getLogger("shrinkly.jobs")

if TYPE_CHECKING:
    from redis import Redis
    from rq.job import Job as RQJob


async def process_job(
    runtime: RuntimeState,
    job_id: str,
    mode: str,
    token_limit: int,
    ocr_language: str,
) -> None:
    """Run analysis, conversion, splitting, and packaging off the event loop."""
    await asyncio.to_thread(
        _process_job_sync,
        runtime,
        job_id,
        mode,
        token_limit,
        ocr_language,
    )


def process_queued_job(
    job_id: str,
    mode: str,
    token_limit: int,
    ocr_language: str,
) -> None:
    """Run an RQ job using Redis-backed state and shared local storage."""
    from app.settings import Settings

    runtime = RuntimeState(Settings.from_environment())
    _process_job_sync(runtime, job_id, mode, token_limit, ocr_language)


def mark_queued_job_failed(
    rq_job: RQJob,
    connection: Redis,
    exception_type: Type[BaseException],
    _exception_value: BaseException,
    _traceback: TracebackType | None,
) -> None:
    """Persist failure when RQ stops a worker job, including timeout kills."""
    from app.settings import Settings

    runtime = RuntimeState(
        Settings.from_environment(),
        redis_client=connection,
    )
    job = runtime.get_job(rq_job.id)
    if job is None:
        return
    if job.cancel_requested:
        _finalize_deletion(runtime, job.job_id)
        return
    timeout = exception_type.__name__ == "JobTimeoutException"
    job.status = "failed"
    job.progress = min(job.progress, 99)
    job.error = (
        "Processing exceeded the configured time limit. Try a smaller PDF."
        if timeout
        else "Processing failed. Check that the PDF is valid and required OCR tools are installed."
    )
    job.completed_at = time.time()
    runtime.save_job(job)
    session = runtime.get_upload(job.upload_id)
    if session is not None and session.directory.is_dir():
        os.utime(session.directory, None)


def _process_job_sync(
    runtime: RuntimeState,
    job_id: str,
    mode: str,
    token_limit: int,
    ocr_language: str,
) -> None:
    """Execute the CPU- and disk-intensive work for one job."""
    with runtime.lock:
        job = runtime.get_job(job_id)
        if job is None:
            return
        job.status = "processing"
        runtime.save_job(job)
        upload = runtime.get_upload(job.upload_id)
        if upload is None:
            job.status = "failed"
            job.error = "The uploaded file is no longer available."
            job.completed_at = time.time()
            runtime.save_job(job)
            return
        source_path = upload.source_path
        output_directory = upload.directory / "job-output"

    try:
        output_directory.mkdir(parents=True, exist_ok=True)
        handler = get_handler(source_path)
        analysis = handler.analyze(source_path)
        _update_job(runtime, job_id, progress=10, analysis=analysis)
        _check_cancelled(runtime, job_id)

        if mode == "compressed_original":
            _update_job(runtime, job_id, progress=25)
            output_path = handler.compress(
                source_path, output_directory / "compressed.pdf"
            )
            if output_path is None:
                raise ShrinklyError("Compressed PDF output is not available.")
            download_filename = "compressed.pdf"
            download_media_type = "application/pdf"
            warnings = [
                "Images above 300 DPI are downsampled toward 150 DPI; "
                "image quality may be reduced."
            ]
        else:
            markdown_path = output_directory / "full.md"

            def report_page_progress(completed: int, total: int) -> None:
                _check_cancelled(runtime, job_id)
                progress = 10 + round(65 * completed / max(total, 1))
                _update_job(runtime, job_id, progress=progress)

            markdown = handler.to_markdown(
                source_path,
                ProcessingOptions(
                    output_path=markdown_path,
                    ocr_language=ocr_language,
                    progress_callback=report_page_progress,
                ),
            )
            _check_cancelled(runtime, job_id)
            warnings = analysis.warnings + markdown.warnings
            _update_job(runtime, job_id, progress=78, warnings=warnings)

            if mode == "text_only":
                output_path = markdown.markdown_path
                download_filename = "ai-ready.md"
                download_media_type = "text/markdown"
            else:
                parts = split_markdown(
                    markdown.markdown_path,
                    output_directory / "parts",
                    token_limit,
                )
                _check_cancelled(runtime, job_id)
                output_path = output_directory / "shrinkly-parts.zip"
                build_package(
                    output_path=output_path,
                    parts=parts,
                    analysis=analysis,
                    source_filename=upload.filename,
                    mode=mode,
                    token_limit=token_limit,
                    warnings=warnings,
                )
                with zipfile.ZipFile(output_path) as archive:
                    if archive.testzip() is not None:
                        raise ShrinklyError("The generated download archive is invalid.")
                download_filename = "ai-ready-parts.zip"
                download_media_type = "application/zip"

        updated = _update_job(
            runtime,
            job_id,
            status="completed",
            progress=100,
            warnings=warnings,
            download_path=output_path,
            download_filename=download_filename,
            download_media_type=download_media_type,
            completed_at=time.time(),
        )
        if not updated:
            _finalize_deletion(runtime, job_id)
    except ShrinklyError as exc:
        if _is_cancelled(runtime, job_id):
            _finalize_deletion(runtime, job_id)
            return
        updated = _update_job(
            runtime,
            job_id,
            status="failed",
            error=str(exc),
            completed_at=time.time(),
        )
        if not updated:
            _finalize_deletion(runtime, job_id)
    except Exception as exc:
        logger.error("Job processing failed (%s).", type(exc).__name__)
        if _is_cancelled(runtime, job_id):
            _finalize_deletion(runtime, job_id)
            return
        updated = _update_job(
            runtime,
            job_id,
            status="failed",
            error="Processing failed. Check that the PDF is valid and required OCR tools are installed.",
            completed_at=time.time(),
        )
        if not updated:
            _finalize_deletion(runtime, job_id)


def _update_job(
    runtime: RuntimeState, job_id: str, **changes: object
) -> bool:
    """Apply job state changes safely from worker threads."""
    with runtime.lock:
        job = runtime.get_job(job_id)
        if job is None:
            return False
        if job.cancel_requested and changes.get("status") in {"completed", "failed"}:
            return False
        for key, value in changes.items():
            setattr(job, key, value)
        runtime.save_job(job)
        session = runtime.get_upload(job.upload_id)
        if "completed_at" in changes:
            if session is not None and session.directory.is_dir():
                os.utime(session.directory, None)
        return True


def _check_cancelled(runtime: RuntimeState, job_id: str) -> None:
    """Stop processing when a user has requested job deletion."""
    with runtime.lock:
        job = runtime.get_job(job_id)
        if job is None or job.cancel_requested:
            raise ShrinklyError("This job was deleted.")


def _is_cancelled(runtime: RuntimeState, job_id: str) -> bool:
    """Check whether deletion was requested while processing."""
    with runtime.lock:
        job = runtime.get_job(job_id)
        return job is None or job.cancel_requested


def _finalize_deletion(runtime: RuntimeState, job_id: str) -> None:
    """Remove a cancelled job's files after open documents have been closed."""
    with runtime.lock:
        job = runtime.get_job(job_id)
        if job is None:
            return
        session = runtime.get_upload(job.upload_id)
        if session is None:
            runtime.remove_job(job_id)
            return
        try:
            if session.directory.parent == runtime.settings.data_dir:
                shutil.rmtree(session.directory, ignore_errors=False)
        except OSError as exc:
            logger.error("Cancelled-file cleanup failed (%s).", type(exc).__name__)
            job.status = "failed"
            job.error = "The job stopped, but its files could not be deleted immediately."
            job.completed_at = time.time()
            runtime.save_job(job)
            return
        runtime.remove_upload(session.upload_id)
        runtime.remove_job(job_id)
