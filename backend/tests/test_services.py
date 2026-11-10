"""Tests for PDF and shared conversion services."""

from __future__ import annotations

import os
import time
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz
import pytest
import fakeredis
from rq.job import Job as RQJob

from app.settings import Settings
from app.handlers.pdf import PDFHandler
from app.models import ProcessingOptions
from app.registry import get_handler
from app.services.cleanup import cleanup_expired
from app.services.errors import ShrinklyError
from app.services.package import build_package
from app.services.pipeline import mark_queued_job_failed
from app.services.split import split_markdown
from app.services.tokens import estimate_tokens
from app.state import Job, RuntimeState, UploadSession


def create_text_pdf(path: Path, pages: int = 1) -> None:
    """Create a small text PDF for deterministic tests."""
    document = fitz.open()
    for page_number in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"Section {page_number + 1}", fontsize=20)
        page.insert_text(
            (72, 110),
            "This is searchable body text used to verify PDF extraction.",
            fontsize=11,
        )
    document.save(path)
    document.close()


def test_token_estimate_rounds_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("1234") == 1
    assert estimate_tokens("12345") == 2


def test_registry_prefers_magic_signature_to_extension(tmp_path: Path) -> None:
    pdf_path = tmp_path / "document.bin"
    create_text_pdf(pdf_path)

    handler = get_handler(pdf_path)

    assert handler.format_name == "pdf"


def test_cleanup_removes_expired_orphaned_job_directory(tmp_path: Path) -> None:
    data_directory = tmp_path / "data"
    orphan = data_directory / str(uuid.uuid4())
    unrelated = data_directory / "keep-this"
    active_upload = data_directory / str(uuid.uuid4())
    orphan.mkdir(parents=True)
    unrelated.mkdir()
    active_upload.mkdir()
    stale_time = time.time() - 7200
    os.utime(orphan, (stale_time, stale_time))
    runtime = RuntimeState(Settings(data_dir=data_directory, expiry_seconds=3600))
    runtime.uploads["active-upload"] = UploadSession(
        upload_id="active-upload",
        filename="source.pdf",
        expected_size=12,
        directory=active_upload,
        source_path=active_upload / "source.pdf",
        created_at=stale_time,
        last_activity_at=time.time() - 10,
    )

    cleanup_expired(
        runtime,
        now=time.time(),
    )

    assert not orphan.exists()
    assert unrelated.is_dir()
    assert active_upload.is_dir()


def test_redis_state_and_rq_queue_survive_runtime_recreation(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        redis_url="redis://unused/0",
        uploads_per_ip_per_hour=2,
    )
    connection = fakeredis.FakeRedis()
    first_runtime = RuntimeState(settings, redis_client=connection)
    session = UploadSession(
        upload_id="upload-1",
        filename="source.pdf",
        expected_size=12,
        directory=settings.data_dir / "upload-1",
        source_path=settings.data_dir / "upload-1" / "source.pdf",
        received_bytes=12,
        next_chunk=1,
        job_id="job-1",
    )
    first_runtime.save_upload(session)
    first_runtime.save_job(
        Job(job_id="job-1", upload_id="upload-1", status="queued")
    )
    first_runtime.enqueue_job("job-1", "split", 1000, "eng")

    restarted_runtime = RuntimeState(settings, redis_client=connection)

    assert restarted_runtime.get_upload("upload-1").received_bytes == 12
    assert restarted_runtime.get_job("job-1").status == "queued"
    assert connection.exists("rq:job:job-1")
    assert restarted_runtime.allow_upload("192.0.2.1")
    assert restarted_runtime.allow_upload("192.0.2.1")
    assert not restarted_runtime.allow_upload("192.0.2.1")

    stale_job = first_runtime.get_job("job-1")
    job = restarted_runtime.get_job("job-1")
    restarted_runtime.request_job_cancellation(job)
    stale_job.progress = 50
    first_runtime.save_job(stale_job)
    assert restarted_runtime.get_job("job-1").cancel_requested
    assert restarted_runtime.get_job("job-1").status == "deleting"
    failed_job = restarted_runtime.get_job("job-1")
    failed_job.status = "failed"
    failed_job.completed_at = time.time()
    restarted_runtime.save_job(failed_job)
    assert restarted_runtime.get_job("job-1").status == "failed"
    queued = connection.hgetall("rq:job:job-1")
    assert queued
    queued_job = RQJob.fetch("job-1", connection=connection)
    queued_job.cancel()
    assert queued_job.get_status(refresh=True).value == "canceled"


def test_rq_timeout_marks_persisted_job_failed(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        redis_url="redis://unused/0",
    )
    connection = fakeredis.FakeRedis()
    runtime = RuntimeState(settings, redis_client=connection)
    session = UploadSession(
        upload_id="upload-timeout",
        filename="source.pdf",
        expected_size=12,
        directory=settings.data_dir / "upload-timeout",
        source_path=settings.data_dir / "upload-timeout" / "source.pdf",
        received_bytes=12,
    )
    runtime.save_upload(session)
    runtime.save_job(Job(job_id="job-timeout", upload_id=session.upload_id))

    class JobTimeoutException(Exception):
        pass

    mark_queued_job_failed(
        SimpleNamespace(id="job-timeout"),
        connection,
        JobTimeoutException,
        JobTimeoutException("timed out"),
        None,
    )

    failed_job = runtime.get_job("job-timeout")
    assert failed_job.status == "failed"
    assert "time limit" in failed_job.error


def test_pdf_analysis_and_markdown(tmp_path: Path) -> None:
    pdf_path = tmp_path / "input.pdf"
    markdown_path = tmp_path / "full.md"
    create_text_pdf(pdf_path, pages=2)

    handler = PDFHandler()
    analysis = handler.analyze(pdf_path)
    result = handler.to_markdown(
        pdf_path,
        ProcessingOptions(output_path=markdown_path, ocr_language="eng"),
    )

    assert analysis.format == "pdf"
    assert analysis.page_count == 2
    assert all(not page.scanned for page in analysis.pages)
    assert result.markdown_path == markdown_path
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Page 1" in markdown
    assert "### Section 1" in markdown
    assert "Section 1" in markdown
    assert "searchable body text" in markdown


def test_basic_ruled_table_is_preserved_as_markdown(tmp_path: Path) -> None:
    pdf_path = tmp_path / "table.pdf"
    markdown_path = tmp_path / "table.md"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 85), "Example data table", fontsize=14)
    for x_position in (50, 180, 310):
        page.draw_line((x_position, 100), (x_position, 170), width=1)
    for y_position in (100, 135, 170):
        page.draw_line((50, y_position), (310, y_position), width=1)
    for point, text in (
        ((60, 122), "Name"),
        ((190, 122), "Value"),
        ((60, 157), "Alpha"),
        ((190, 157), "42"),
    ):
        page.insert_text(point, text)
    document.save(pdf_path)
    document.close()

    PDFHandler().to_markdown(
        pdf_path,
        ProcessingOptions(output_path=markdown_path, ocr_language="eng"),
    )
    markdown = markdown_path.read_text(encoding="utf-8")

    assert "| Name | Value |" in markdown
    assert "| Alpha | 42 |" in markdown


def test_pdf_analysis_marks_blank_pages_as_scanned(tmp_path: Path) -> None:
    pdf_path = tmp_path / "mixed.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A page with enough real extractable text.")
    document.new_page()
    document.save(pdf_path)
    document.close()

    analysis = PDFHandler().analyze(pdf_path)

    assert [page.scanned for page in analysis.pages] == [False, True]
    assert any("OCR" in warning for warning in analysis.warnings)


def test_corrupt_and_password_protected_pdfs_fail_clearly(tmp_path: Path) -> None:
    corrupt_path = tmp_path / "corrupt.pdf"
    corrupt_path.write_bytes(b"%PDF-1.7\nnot a valid PDF structure")
    with pytest.raises(ShrinklyError, match="damaged"):
        PDFHandler().analyze(corrupt_path)

    protected_path = tmp_path / "protected.pdf"
    document = fitz.open()
    document.new_page().insert_text((72, 72), "Protected page contents.")
    document.save(
        protected_path,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw="user-password",
        owner_pw="owner-password",
    )
    document.close()

    with pytest.raises(ShrinklyError, match="password-protected"):
        PDFHandler().analyze(protected_path)


def test_mixed_500_page_pdf_ocr_attempts_only_scanned_pages(
    tmp_path: Path, monkeypatch
) -> None:
    pdf_path = tmp_path / "mixed-500.pdf"
    markdown_path = tmp_path / "mixed-500.md"
    document = fitz.open()
    for page_number in range(500):
        page = document.new_page()
        if page_number == 249:
            pixmap = fitz.Pixmap(
                fitz.csRGB,
                (0, 0, 40, 40),
                False,
            )
            pixmap.clear_with(0xFFFFFF)
            page.insert_image(
                fitz.Rect(72, 72, 300, 300), stream=pixmap.tobytes("png")
            )
        else:
            page.insert_text(
                (72, 72),
                f"Page {page_number + 1} has enough searchable text to avoid OCR.",
            )
    document.save(pdf_path)
    document.close()
    ocr_pages: list[int] = []

    def fake_ocr(page, **_options):
        ocr_pages.append(page.number + 1)
        return page.get_textpage()

    monkeypatch.setattr(fitz.Page, "get_textpage_ocr", fake_ocr)
    handler = PDFHandler()
    analysis = handler.analyze(pdf_path)
    handler.to_markdown(
        pdf_path,
        ProcessingOptions(output_path=markdown_path, ocr_language="eng"),
    )

    assert analysis.page_count == 500
    assert [page.page_number for page in analysis.pages if page.scanned] == [250]
    assert ocr_pages == [250]
    assert markdown_path.stat().st_size > 10_000


def test_compress_downsamples_high_dpi_images_to_150_dpi_target(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "image-heavy.pdf"
    compressed_path = tmp_path / "image-compressed.pdf"
    pixmap = fitz.Pixmap(fitz.csRGB, (0, 0, 1800, 1800), False)
    pixmap.clear_with(0xCC2A55)
    document = fitz.open()
    document.new_page(width=612, height=792).insert_image(
        fitz.Rect(50, 50, 300, 300),
        stream=pixmap.tobytes("png"),
    )
    document.save(pdf_path)
    document.close()

    PDFHandler().compress(pdf_path, compressed_path)

    with fitz.open(pdf_path) as original, fitz.open(compressed_path) as compressed:
        original_image = original[0].get_image_info(xrefs=True)[0]
        compressed_image = compressed[0].get_image_info(xrefs=True)[0]
        assert original_image["width"] == 1800
        assert compressed_image["width"] < original_image["width"]
        assert compressed_image["width"] <= 900
        assert compressed[0].get_images(full=True)
    assert compressed_path.stat().st_size < pdf_path.stat().st_size


def test_compress_strips_metadata_and_preserves_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "input.pdf"
    compressed_path = tmp_path / "compressed.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A document that remains readable after compression.")
    document.set_metadata({"title": "Private document title"})
    document.save(pdf_path)
    document.close()

    result = PDFHandler().compress(pdf_path, compressed_path)

    assert result == compressed_path
    with fitz.open(compressed_path) as compressed:
        assert compressed.page_count == 1
        assert "Private document title" not in str(compressed.metadata)
        assert "remains readable" in compressed[0].get_text()


def test_split_parts_stay_under_token_limit_and_package(tmp_path: Path) -> None:
    markdown_path = tmp_path / "full.md"
    parts_directory = tmp_path / "parts"
    markdown_path.write_text(
        "## Page 1\n\n# First section\n\n"
        + ("A searchable paragraph with details. " * 90)
        + "\n\n# Second section\n\n"
        + ("Another section with more detail. " * 90),
        encoding="utf-8",
    )
    analysis = PDFHandler().analyze(_pdf_fixture(tmp_path))

    parts = split_markdown(markdown_path, parts_directory, token_limit=128)

    assert len(parts) > 1
    assert all(part.token_count <= 128 for part in parts)
    archive_path = tmp_path / "result.zip"
    build_package(
        output_path=archive_path,
        parts=parts,
        analysis=analysis,
        source_filename="source.pdf",
        mode="split",
        token_limit=128,
        warnings=[],
    )
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "full.md" not in names
        assert len([name for name in names if name.startswith("parts/")]) == len(parts)


def _pdf_fixture(tmp_path: Path) -> Path:
    pdf_path = tmp_path / "source.pdf"
    create_text_pdf(pdf_path)
    return pdf_path
