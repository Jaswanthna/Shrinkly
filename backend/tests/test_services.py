"""Tests for PDF and shared conversion services."""

from __future__ import annotations

import os
import time
import uuid
import zipfile
from pathlib import Path

import pymupdf as fitz

from app.settings import Settings
from app.handlers.pdf import PDFHandler
from app.models import ProcessingOptions
from app.services.cleanup import cleanup_expired
from app.services.package import build_package
from app.services.split import split_markdown
from app.services.tokens import estimate_tokens
from app.state import RuntimeState


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


def test_cleanup_removes_expired_orphaned_job_directory(tmp_path: Path) -> None:
    data_directory = tmp_path / "data"
    orphan = data_directory / str(uuid.uuid4())
    unrelated = data_directory / "keep-this"
    orphan.mkdir(parents=True)
    unrelated.mkdir()
    stale_time = time.time() - 7200
    os.utime(orphan, (stale_time, stale_time))

    cleanup_expired(
        RuntimeState(Settings(data_dir=data_directory, expiry_seconds=3600)),
        now=time.time(),
    )

    assert not orphan.exists()
    assert unrelated.is_dir()


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
