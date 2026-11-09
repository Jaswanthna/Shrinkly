"""Stream Markdown into bounded parts while preferring heading boundaries."""

from __future__ import annotations

import re
from pathlib import Path

from app.models import PartFile
from app.services.errors import ShrinklyError
from app.services.tokens import estimate_tokens_from_characters


_PAGE_PATTERN = re.compile(r"^## Page (\d+)\s*$")


def split_markdown(
    markdown_path: Path, parts_directory: Path, token_limit: int
) -> list[PartFile]:
    """Create Markdown part files whose headers and content fit the token limit."""
    if token_limit < 64:
        raise ShrinklyError("Choose a part size of at least 64 tokens.")

    parts_directory.mkdir(parents=True, exist_ok=True)
    payload_character_limit = token_limit * 4 - 128
    if payload_character_limit < 1:
        raise ShrinklyError("The selected part size is too small for part headers.")

    pending: list[str] = []
    pending_characters = 0
    page_start: int | None = None
    page_end: int | None = None
    raw_parts: list[tuple[Path, int | None, int | None]] = []

    def flush() -> None:
        nonlocal pending, pending_characters, page_start, page_end
        if not pending and raw_parts:
            return
        raw_path = parts_directory / f".raw-{len(raw_parts) + 1:04d}.md"
        raw_path.write_text("".join(pending), encoding="utf-8")
        raw_parts.append((raw_path, page_start, page_end))
        pending = []
        pending_characters = 0
        page_start = None
        page_end = None

    with markdown_path.open("r", encoding="utf-8") as source:
        for line in source:
            page_match = _PAGE_PATTERN.match(line.rstrip("\r\n"))
            if page_match:
                page_number = int(page_match.group(1))
                if (
                    pending_characters >= payload_character_limit // 2
                    and pending
                ):
                    flush()
                if page_start is None:
                    page_start = page_number
                page_end = page_number

            if line.startswith("#") and pending_characters >= payload_character_limit // 2:
                flush()

            remaining = line
            while remaining:
                available = payload_character_limit - pending_characters
                if available == 0:
                    flush()
                    available = payload_character_limit
                piece = remaining[:available]
                pending.append(piece)
                pending_characters += len(piece)
                remaining = remaining[len(piece) :]
                if remaining:
                    flush()

    if pending or not raw_parts:
        flush()

    part_count = len(raw_parts)
    output_parts: list[PartFile] = []
    for index, (raw_path, first_page, last_page) in enumerate(raw_parts, start=1):
        page_label = (
            f"; pages {first_page}-{last_page}"
            if first_page is not None and last_page is not None
            else ""
        )
        header = f"<!-- Part {index} of {part_count}{page_label} -->\n\n"
        output_path = parts_directory / f"part-{index:02d}.md"
        with output_path.open("wb") as target:
            target.write(header.encode("utf-8"))
            with raw_path.open("rb") as raw_source:
                while chunk := raw_source.read(1024 * 1024):
                    target.write(chunk)
        raw_path.unlink()
        token_count = _estimate_file_tokens(output_path)
        if token_count > token_limit:
            raise ShrinklyError(
                "A generated part exceeded the selected token limit. "
                "Choose a larger part size and retry."
            )
        output_parts.append(
            PartFile(
                path=output_path,
                token_count=token_count,
                page_start=first_page,
                page_end=last_page,
            )
        )
    return output_parts


def _estimate_file_tokens(path: Path) -> int:
    """Estimate a file's tokens without holding the entire part in memory."""
    character_count = 0
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            character_count += len(line)
    return estimate_tokens_from_characters(character_count)
