"""Typed data structures shared by handlers and services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class PageAnalysis:
    """Analysis details for one PDF page."""

    page_number: int
    scanned: bool
    text_characters: int


@dataclass(frozen=True)
class AnalysisResult:
    """Format-independent file analysis output."""

    format: str
    page_count: int
    file_size_bytes: int
    estimated_tokens: int
    pages: list[PageAnalysis]
    warnings: list[str]


@dataclass(frozen=True)
class ProcessingOptions:
    """Options needed while converting a document."""

    output_path: Path
    ocr_language: str
    progress_callback: ProgressCallback | None = None


@dataclass(frozen=True)
class MarkdownResult:
    """Location and warnings for generated Markdown."""

    markdown_path: Path
    warnings: list[str]


@dataclass(frozen=True)
class PartFile:
    """A generated Markdown part and its metadata."""

    path: Path
    token_count: int
    page_start: int | None
    page_end: int | None
