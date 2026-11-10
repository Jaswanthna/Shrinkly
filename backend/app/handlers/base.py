"""Base contracts for file-format handlers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.models import AnalysisResult, MarkdownResult, ProcessingOptions


class FormatHandler(ABC):
    """Common interface implemented by supported file formats."""

    extensions: list[str] = []
    mime_types: list[str] = []
    magic_signatures: tuple[bytes, ...] = ()
    format_name: str

    @abstractmethod
    def analyze(self, path: Path) -> AnalysisResult:
        """Inspect the input without modifying it."""

    @abstractmethod
    def to_markdown(
        self, path: Path, options: ProcessingOptions
    ) -> MarkdownResult:
        """Write a Markdown representation to the configured output path."""

    @abstractmethod
    def compress(self, path: Path, options: Path) -> Path | None:
        """Write a compressed original when the format supports it."""
