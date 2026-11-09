"""HTTP request and response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UploadInitRequest(BaseModel):
    """Metadata required before accepting upload chunks."""

    filename: str = Field(min_length=1, max_length=255)
    file_size: int = Field(gt=0)


class UploadInitResponse(BaseModel):
    """Chunked upload information returned to the client."""

    upload_id: str
    chunk_size: int
    total_chunks: int
    expires_in_seconds: int


class JobCreateRequest(BaseModel):
    """Conversion options for an uploaded file."""

    upload_id: str = Field(min_length=1, max_length=64)
    mode: Literal["text_only", "split", "compressed_original"] = "split"
    target_ai: Literal["chatgpt", "claude", "gemini", "custom"] = "chatgpt"
    custom_token_limit: int | None = Field(default=None, ge=64, le=1_000_000)
    ocr_language: str = Field(
        default="eng",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_+-]+$",
    )


class JobCreateResponse(BaseModel):
    """New job identifier and initial state."""

    job_id: str
    status: str


class PageAnalysisResponse(BaseModel):
    """Analysis information for one page."""

    page_number: int
    scanned: bool
    text_characters: int


class AnalysisResponse(BaseModel):
    """Safe-to-serialize analysis summary."""

    format: str
    page_count: int
    file_size_bytes: int
    estimated_tokens: int
    pages: list[PageAnalysisResponse]
    warnings: list[str]


class JobStatusResponse(BaseModel):
    """Progress and results for a processing job."""

    job_id: str
    status: str
    progress: int
    detected_format: str
    warnings: list[str]
    analysis: AnalysisResponse | None = None
    error: str | None = None
