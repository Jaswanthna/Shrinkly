"""Format handler lookup, preferring file signatures over file extensions."""

from __future__ import annotations

from pathlib import Path

from app.handlers.base import FormatHandler
from app.handlers.pdf import PDFHandler
from app.services.errors import ShrinklyError


_HANDLERS: tuple[FormatHandler, ...] = (PDFHandler(),)


def get_handler(path: Path) -> FormatHandler:
    """Resolve a handler by magic bytes first, then by extension."""
    try:
        with path.open("rb") as source:
            signature = source.read(1024)
    except OSError as exc:
        raise ShrinklyError("The uploaded file could not be read.") from exc

    if b"%PDF-" in signature:
        return next(handler for handler in _HANDLERS if handler.format_name == "pdf")

    extension = path.suffix.lower()
    for handler in _HANDLERS:
        if extension in handler.extensions:
            raise ShrinklyError(
                "The file contents do not match the .pdf extension. "
                "Upload a valid PDF file."
            )
    raise ShrinklyError("Unsupported file type. Currently supported: PDF.")
