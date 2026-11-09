"""Upload and background-job API routes."""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse

from app.models import AnalysisResult
from app.registry import get_handler
from app.schemas import (
    AnalysisResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobStatusResponse,
    PageAnalysisResponse,
    UploadInitRequest,
    UploadInitResponse,
)
from app.services.errors import ShrinklyError
from app.services.pipeline import process_job
from app.state import Job, RuntimeState, UploadSession


router = APIRouter(prefix="/api")
_AI_LIMITS_PATH = Path(__file__).resolve().parents[1] / "config" / "ai_limits.json"


@router.post("/upload/init", response_model=UploadInitResponse)
async def initialize_upload(
    payload: UploadInitRequest, request: Request
) -> UploadInitResponse:
    """Reserve a bounded temporary directory for a chunked upload."""
    runtime = _runtime(request)
    client_ip = request.client.host if request.client else "unknown"
    if not runtime.allow_upload(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Upload limit reached. Try again later.",
        )
    if payload.file_size > runtime.settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File is larger than the {runtime.settings.max_upload_bytes}-byte limit.",
        )

    upload_id = str(uuid.uuid4())
    directory = runtime.settings.data_dir / upload_id
    directory.mkdir(parents=True, exist_ok=False)
    session = UploadSession(
        upload_id=upload_id,
        filename=payload.filename,
        expected_size=payload.file_size,
        directory=directory,
        source_path=directory / "source.pdf",
    )
    with runtime.lock:
        runtime.uploads[upload_id] = session
    chunk_size = runtime.settings.chunk_bytes
    return UploadInitResponse(
        upload_id=upload_id,
        chunk_size=chunk_size,
        total_chunks=math.ceil(payload.file_size / chunk_size),
        expires_in_seconds=runtime.settings.expiry_seconds,
    )


@router.put("/upload/{upload_id}/chunk")
async def upload_chunk(
    upload_id: str, chunk_index: int, request: Request
) -> dict[str, int | bool]:
    """Accept exactly one ordered, bounded binary upload chunk."""
    runtime = _runtime(request)
    session = _get_upload(runtime, upload_id)
    async with session.lock:
        if session.job_id is not None:
            raise HTTPException(status_code=409, detail="This upload was already submitted.")
        expected_chunks = math.ceil(
            session.expected_size / runtime.settings.chunk_bytes
        )
        if chunk_index != session.next_chunk or chunk_index >= expected_chunks:
            raise HTTPException(
                status_code=409,
                detail=f"Expected upload chunk {session.next_chunk}.",
            )
        expected_chunk_size = min(
            runtime.settings.chunk_bytes,
            session.expected_size - session.received_bytes,
        )
        temporary_path = session.directory / f".chunk-{chunk_index:08d}.tmp"
        final_path = session.directory / f"chunk-{chunk_index:08d}.part"
        received = 0
        try:
            with temporary_path.open("wb") as target:
                async for body_chunk in request.stream():
                    received += len(body_chunk)
                    if received > expected_chunk_size:
                        raise HTTPException(
                            status_code=413,
                            detail="Upload chunk exceeds its expected size.",
                        )
                    target.write(body_chunk)
            if received != expected_chunk_size:
                raise HTTPException(
                    status_code=400,
                    detail="Upload chunk size does not match the expected size.",
                )
            temporary_path.replace(final_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        session.received_bytes += received
        session.next_chunk += 1
        return {
            "chunk_index": chunk_index,
            "received_bytes": session.received_bytes,
            "complete": session.received_bytes == session.expected_size,
        }


@router.post("/jobs", response_model=JobCreateResponse, status_code=202)
async def create_job(
    payload: JobCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> JobCreateResponse:
    """Validate a completed upload and queue its conversion."""
    runtime = _runtime(request)
    session = _get_upload(runtime, payload.upload_id)
    async with session.lock:
        if session.job_id is not None:
            raise HTTPException(status_code=409, detail="This upload was already submitted.")
        if session.received_bytes != session.expected_size:
            raise HTTPException(status_code=409, detail="The upload is not complete.")

        token_limit = _resolve_token_limit(payload)
        try:
            await asyncio.to_thread(_assemble_upload, runtime, session)
            handler = get_handler(session.source_path)
        except ShrinklyError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail="The uploaded file could not be assembled."
            ) from exc
        expected_chunks = math.ceil(
            session.expected_size / runtime.settings.chunk_bytes
        )
        for index in range(expected_chunks):
            (session.directory / f"chunk-{index:08d}.part").unlink(missing_ok=True)
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id, upload_id=session.upload_id, format=handler.format_name)
        with runtime.lock:
            session.job_id = job_id
            runtime.jobs[job_id] = job
        background_tasks.add_task(
            process_job,
            runtime,
            job_id,
            payload.mode,
            token_limit,
            payload.ocr_language,
        )
    return JobCreateResponse(job_id=job_id, status="queued")


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str, request: Request) -> JobStatusResponse:
    """Return status, progress, analysis, and warnings for a job."""
    runtime = _runtime(request)
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found or expired.")
        return _serialize_job(job)


@router.get("/jobs/{job_id}/download")
async def download_job(job_id: str, request: Request) -> FileResponse:
    """Stream the completed result archive to the client."""
    runtime = _runtime(request)
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found or expired.")
        if job.status != "completed" or job.download_path is None:
            raise HTTPException(status_code=409, detail="The result is not ready yet.")
        download_path = job.download_path
    if not download_path.is_file():
        raise HTTPException(status_code=404, detail="The result file is no longer available.")
    return FileResponse(
        download_path,
        media_type="application/zip",
        filename="shrinkly-ai-ready.zip",
    )


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str, request: Request) -> Response:
    """Delete an upload and its result, cancelling active work if needed."""
    runtime = _runtime(request)
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found or already deleted.")
        session = runtime.uploads.get(job.upload_id)
        if job.status == "processing":
            job.cancel_requested = True
            job.status = "deleting"
            return Response(status_code=202)
        if session is not None:
            if session.directory.parent == runtime.settings.data_dir:
                shutil.rmtree(session.directory, ignore_errors=False)
            runtime.uploads.pop(session.upload_id, None)
        runtime.jobs.pop(job_id, None)
    return Response(status_code=204)


def _assemble_upload(runtime: RuntimeState, session: UploadSession) -> None:
    """Join verified chunks into the source file without loading it in memory."""
    expected_chunks = math.ceil(
        session.expected_size / runtime.settings.chunk_bytes
    )
    with session.source_path.open("wb") as destination:
        for index in range(expected_chunks):
            part_path = session.directory / f"chunk-{index:08d}.part"
            with part_path.open("rb") as part:
                shutil.copyfileobj(part, destination, length=1024 * 1024)
    if session.source_path.stat().st_size != session.expected_size:
        session.source_path.unlink(missing_ok=True)
        raise OSError("Assembled file size mismatch.")


def _resolve_token_limit(payload: JobCreateRequest) -> int:
    """Load a configured preset or require an explicit custom token limit."""
    if payload.target_ai == "custom":
        if payload.custom_token_limit is None:
            raise HTTPException(
                status_code=422,
                detail="custom_token_limit is required for a custom target.",
            )
        return payload.custom_token_limit
    try:
        limits = json.loads(_AI_LIMITS_PATH.read_text(encoding="utf-8"))
        return int(limits[payload.target_ai]["token_limit"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500, detail="AI target limits are not configured correctly."
        ) from exc


def _serialize_job(job: Job) -> JobStatusResponse:
    """Convert internal job state to the public API shape."""
    analysis = None
    if job.analysis is not None:
        result: AnalysisResult = job.analysis
        analysis = AnalysisResponse(
            format=result.format,
            page_count=result.page_count,
            file_size_bytes=result.file_size_bytes,
            estimated_tokens=result.estimated_tokens,
            pages=[
                PageAnalysisResponse(
                    page_number=page.page_number,
                    scanned=page.scanned,
                    text_characters=page.text_characters,
                )
                for page in result.pages
            ],
            warnings=result.warnings,
        )
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        detected_format=job.format,
        warnings=job.warnings,
        analysis=analysis,
        error=job.error,
    )


def _runtime(request: Request) -> RuntimeState:
    """Retrieve app-scoped state."""
    return request.app.state.runtime


def _get_upload(runtime: RuntimeState, upload_id: str) -> UploadSession:
    """Return a known upload without exposing storage paths."""
    with runtime.lock:
        session = runtime.uploads.get(upload_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Upload not found or expired.")
    return session
