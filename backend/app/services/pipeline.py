"""Background processing pipeline for uploaded files."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import zipfile

from app.models import ProcessingOptions
from app.registry import get_handler
from app.services.errors import ShrinklyError
from app.services.package import build_package
from app.services.split import split_markdown
from app.state import Job, RuntimeState


logger = logging.getLogger("shrinkly.jobs")


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


def _process_job_sync(
    runtime: RuntimeState,
    job_id: str,
    mode: str,
    token_limit: int,
    ocr_language: str,
) -> None:
    """Execute the CPU- and disk-intensive work for one job."""
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None:
            return
        job.status = "processing"
        upload = runtime.uploads.get(job.upload_id)
        if upload is None:
            job.status = "failed"
            job.error = "The uploaded file is no longer available."
            job.completed_at = time.time()
            return
        source_path = upload.source_path
        output_directory = upload.directory / "job-output"
        output_directory.mkdir(parents=True, exist_ok=True)

    try:
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
            warnings: list[str] = []
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


def _update_job(runtime: RuntimeState, job_id: str, **changes: object) -> bool:
    """Apply job state changes safely from worker threads."""
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None:
            return False
        if job.cancel_requested and changes.get("status") in {"completed", "failed"}:
            return False
        for key, value in changes.items():
            setattr(job, key, value)
        if "completed_at" in changes:
            session = runtime.uploads.get(job.upload_id)
            if session is not None and session.directory.is_dir():
                os.utime(session.directory, None)
        return True


def _check_cancelled(runtime: RuntimeState, job_id: str) -> None:
    """Stop processing when a user has requested job deletion."""
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None or job.cancel_requested:
            raise ShrinklyError("This job was deleted.")


def _is_cancelled(runtime: RuntimeState, job_id: str) -> bool:
    """Check whether deletion was requested while processing."""
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        return job is None or job.cancel_requested


def _finalize_deletion(runtime: RuntimeState, job_id: str) -> None:
    """Remove a cancelled job's files after open documents have been closed."""
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None:
            return
        session = runtime.uploads.get(job.upload_id)
        if session is None:
            runtime.jobs.pop(job_id, None)
            return
        try:
            if session.directory.parent == runtime.settings.data_dir:
                shutil.rmtree(session.directory, ignore_errors=False)
        except OSError as exc:
            logger.error("Cancelled-file cleanup failed (%s).", type(exc).__name__)
            job.status = "failed"
            job.error = "The job stopped, but its files could not be deleted immediately."
            job.completed_at = time.time()
            return
        runtime.uploads.pop(session.upload_id, None)
        runtime.jobs.pop(job_id, None)
