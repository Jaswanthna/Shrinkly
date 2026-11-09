"""PDF analysis, OCR, Markdown extraction, and compression."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pymupdf as fitz
from app.handlers.base import FormatHandler
from app.models import (
    AnalysisResult,
    MarkdownResult,
    PageAnalysis,
    ProcessingOptions,
)
from app.services.errors import ShrinklyError
from app.services.tokens import estimate_tokens_from_characters


_MIN_TEXT_CHARACTERS = 20
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_+-]{1,32}$")


class PDFHandler(FormatHandler):
    """Handler for text, scanned, and mixed PDFs."""

    extensions = [".pdf"]
    mime_types = ["application/pdf"]
    format_name = "pdf"

    def analyze(self, path: Path) -> AnalysisResult:
        """Report page count and identify pages that need OCR."""
        try:
            with fitz.open(path) as document:
                self._require_unlocked(document)
                pages: list[PageAnalysis] = []
                total_characters = 0
                for page_number, page in enumerate(document, start=1):
                    text = page.get_text("text", sort=True)
                    characters = len(text.strip())
                    total_characters += characters
                    pages.append(
                        PageAnalysis(
                            page_number=page_number,
                            scanned=characters < _MIN_TEXT_CHARACTERS,
                            text_characters=characters,
                        )
                    )
        except ShrinklyError:
            raise
        except (fitz.FileDataError, RuntimeError, ValueError) as exc:
            raise ShrinklyError(
                "This PDF is damaged or could not be opened."
            ) from exc

        scanned_pages = sum(page.scanned for page in pages)
        warnings = (
            [f"{scanned_pages} page(s) require OCR."]
            if scanned_pages
            else []
        )
        return AnalysisResult(
            format=self.format_name,
            page_count=len(pages),
            file_size_bytes=path.stat().st_size,
            estimated_tokens=estimate_tokens_from_characters(total_characters),
            pages=pages,
            warnings=warnings,
        )

    def to_markdown(
        self, path: Path, options: ProcessingOptions
    ) -> MarkdownResult:
        """Write page-wise Markdown, OCRing only pages without extractable text."""
        if not _LANGUAGE_PATTERN.fullmatch(options.ocr_language):
            raise ShrinklyError("The selected OCR language is invalid.")

        warnings: list[str] = []
        try:
            with fitz.open(path) as document, options.output_path.open(
                "w", encoding="utf-8", newline="\n"
            ) as output:
                self._require_unlocked(document)
                for page_index, page in enumerate(document):
                    page_number = page_index + 1
                    text = page.get_text("text", sort=True)
                    tables = []
                    try:
                        tables = self._extract_tables(page)
                    except (RuntimeError, ValueError):
                        warnings.append(
                            f"A table could not be detected on page {page_number}."
                        )
                    if len(text.strip()) < _MIN_TEXT_CHARACTERS:
                        try:
                            text_page = page.get_textpage_ocr(
                                language=options.ocr_language,
                                dpi=300,
                                full=True,
                            )
                            page_dict = page.get_text(
                                "dict", textpage=text_page, sort=True
                            )
                            blocks, baseline = self._extract_blocks(
                                page_dict, [bbox for bbox, _ in tables]
                            )
                        except (RuntimeError, ValueError) as exc:
                            raise ShrinklyError(
                                "OCR could not run. Install Tesseract and the "
                                f"'{options.ocr_language}' language data, then retry."
                            ) from exc
                        if not text.strip():
                            warnings.append(
                                f"OCR returned no readable text for page {page_number}."
                            )
                    else:
                        page_dict = page.get_text("dict", sort=True)
                        blocks, baseline = self._extract_blocks(
                            page_dict, [bbox for bbox, _ in tables]
                        )

                    output.write(f"## Page {page_number}\n\n")
                    blocks.extend(
                        (bbox[1], table_markdown, 0)
                        for bbox, table_markdown in tables
                    )
                    output.write(self._format_page(blocks, baseline))
                    output.write("\n\n")
                    if options.progress_callback is not None:
                        options.progress_callback(page_index + 1, len(document))
        except ShrinklyError:
            raise
        except (fitz.FileDataError, OSError, RuntimeError, ValueError) as exc:
            raise ShrinklyError(
                "The PDF could not be converted. Check that it is not damaged "
                "or password-protected."
            ) from exc

        warnings.append(
            "Complex tables, charts, and formulas may not retain their original layout."
        )
        return MarkdownResult(markdown_path=options.output_path, warnings=warnings)

    def compress(self, path: Path, options: Path) -> Path | None:
        """Rewrite a PDF with compressed streams and remove document metadata."""
        try:
            with fitz.open(path) as document:
                self._require_unlocked(document)
                document.set_metadata({})
                document.save(
                    options,
                    garbage=4,
                    deflate=True,
                    deflate_images=True,
                    deflate_fonts=True,
                )
        except (fitz.FileDataError, OSError, RuntimeError, ValueError) as exc:
            raise ShrinklyError(
                "The original PDF could not be compressed. It may be damaged "
                "or password-protected."
            ) from exc
        return options

    @staticmethod
    def _require_unlocked(document: fitz.Document) -> None:
        """Reject encrypted PDFs without attempting to bypass their password."""
        if document.needs_pass:
            raise ShrinklyError(
                "This PDF is password-protected. Remove the password and upload it again."
            )

    @staticmethod
    def _extract_blocks(
        page_dict: dict,
        ignored_regions: list[tuple[float, float, float, float]],
    ) -> tuple[list[tuple[float, str, float]], float]:
        """Extract text blocks and identify likely headings from font sizes."""
        blocks: list[tuple[float, str, float]] = []
        font_sizes: list[float] = []
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            bounds = block.get("bbox", (0, 0, 0, 0))
            center_x = (bounds[0] + bounds[2]) / 2
            center_y = (bounds[1] + bounds[3]) / 2
            if any(
                left <= center_x <= right and top <= center_y <= bottom
                for left, top, right, bottom in ignored_regions
            ):
                continue
            lines: list[str] = []
            block_sizes: list[float] = []
            for line in block.get("lines", []):
                line_text = "".join(
                    span.get("text", "") for span in line.get("spans", [])
                ).strip()
                if line_text:
                    lines.append(line_text)
                block_sizes.extend(
                    float(span.get("size", 0))
                    for span in line.get("spans", [])
                    if span.get("text", "").strip()
                )
            block_text = "\n".join(lines).strip()
            if block_text and block_sizes:
                size = max(block_sizes)
                blocks.append((float(bounds[1]), block_text, size))
                font_sizes.extend(block_sizes)

        if not font_sizes:
            return [], 0
        frequency = Counter(font_sizes)
        baseline = min(frequency, key=lambda size: (-frequency[size], size))
        return blocks, baseline

    @staticmethod
    def _extract_tables(
        page: fitz.Page,
    ) -> list[tuple[tuple[float, float, float, float], str]]:
        """Convert simple PyMuPDF table detections into Markdown tables."""
        tables = []
        for table in page.find_tables().tables:
            rows = table.extract()
            if not rows:
                continue
            column_count = max((len(row) for row in rows), default=0)
            if column_count == 0:
                continue
            normalized = [
                [
                    str(cell or "").replace("|", r"\|").replace("\n", " ").strip()
                    for cell in row
                ]
                + [""] * (column_count - len(row))
                for row in rows
            ]
            header = normalized[0]
            if not any(header):
                header = [f"Column {index + 1}" for index in range(column_count)]
            markdown_rows = [
                "| " + " | ".join(header) + " |",
                "| " + " | ".join("---" for _ in range(column_count)) + " |",
            ]
            markdown_rows.extend(
                "| " + " | ".join(row) + " |" for row in normalized[1:]
            )
            bounds = tuple(float(value) for value in table.bbox)
            tables.append((bounds, "\n".join(markdown_rows)))
        return tables

    @staticmethod
    def _format_page(
        blocks: list[tuple[float, str, float]], baseline: float
    ) -> str:
        """Add lightweight Markdown headings and list markers to extracted text."""
        if not blocks:
            return "_No extractable text on this page._"
        paragraphs: list[str] = []
        for _, text, size in sorted(blocks, key=lambda block: block[0]):
            lines: list[str] = []
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped:
                    lines.append("")
                elif stripped.startswith(("• ", "▪ ", "◦ ")):
                    lines.append(f"- {stripped[2:].strip()}")
                else:
                    lines.append(stripped)
            paragraph = "\n".join(lines)
            if baseline > 0 and size >= baseline * 1.3:
                paragraphs.append(f"### {paragraph.replace(chr(10), ' ')}")
            else:
                paragraphs.append(paragraph)
        return "\n\n".join(paragraphs)
