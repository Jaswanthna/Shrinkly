"""ZIP creation and manifest generation."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.models import AnalysisResult, PartFile
from app.services.tokens import estimate_tokens_from_characters


def build_package(
    output_path: Path,
    parts: list[PartFile],
    analysis: AnalysisResult,
    source_filename: str,
    mode: str,
    token_limit: int,
    warnings: list[str],
) -> None:
    """Write only split Markdown parts and their manifest to a ZIP."""
    manifest = {
        "source_file": Path(source_filename).name,
        "format": analysis.format,
        "mode": mode,
        "source_size_bytes": analysis.file_size_bytes,
        "page_count": analysis.page_count,
        "estimated_tokens": analysis.estimated_tokens,
        "selected_token_limit": token_limit,
        "warnings": list(dict.fromkeys(warnings)),
        "parts": [
            {
                "file": f"parts/{part.path.name}",
                "estimated_tokens": part.token_count,
                "page_start": part.page_start,
                "page_end": part.page_end,
            }
            for part in parts
        ],
    }

    with zipfile.ZipFile(
        output_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for part in parts:
            archive.write(part.path, f"parts/{part.path.name}")
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )


def estimate_markdown_tokens(path: Path) -> int:
    """Estimate tokens in a Markdown file using the configured heuristic."""
    characters = 0
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            characters += len(line)
    return estimate_tokens_from_characters(characters)
