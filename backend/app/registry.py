"""Format handler lookup, preferring file signatures over file extensions."""

from __future__ import annotations

from pathlib import Path

from app.handlers.base import FormatHandler
from app.handlers.pdf import PDFHandler
from app.services.errors import ShrinklyError


_HANDLERS: dict[str, FormatHandler] = {}


def register_handler(handler: FormatHandler) -> None:
    """Register a format handler once for signature and extension detection."""
    if handler.format_name in _HANDLERS:
        raise ValueError(f"A handler for {handler.format_name!r} is already registered.")
    _HANDLERS[handler.format_name] = handler


register_handler(PDFHandler())


def get_handler(path: Path) -> FormatHandler:
    """Resolve a handler by magic bytes first, then by extension."""
    try:
        with path.open("rb") as source:
            signature = source.read(1024)
    except OSError as exc:
        raise ShrinklyError("The uploaded file could not be read.") from exc

    for handler in _HANDLERS.values():
        if any(magic in signature for magic in handler.magic_signatures):
            return handler

    extension = path.suffix.lower()
    for handler in _HANDLERS.values():
        if extension in handler.extensions:
            raise ShrinklyError(
                f"The file contents do not match the {extension} extension. "
                f"Upload a valid {handler.format_name.upper()} file."
            )
    supported = ", ".join(supported_extensions())
    raise ShrinklyError(
        f"Unsupported file type. Currently supported: {supported or 'none'}."
    )


def supported_extensions() -> list[str]:
    """Return unique supported extensions for future API error messages."""
    return sorted(
        {
            extension
            for handler in _HANDLERS.values()
            for extension in handler.extensions
        }
    )
