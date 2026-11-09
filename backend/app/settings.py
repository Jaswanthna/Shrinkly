"""Runtime settings loaded from environment variables."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Application limits and local storage configuration."""

    data_dir: Path
    max_upload_bytes: int = 1024 * 1024 * 1024
    chunk_bytes: int = 8 * 1024 * 1024
    expiry_seconds: int = 60 * 60
    uploads_per_ip_per_hour: int = 20

    def __post_init__(self) -> None:
        """Reject invalid limits before creating storage or accepting requests."""
        if (
            self.max_upload_bytes <= 0
            or self.chunk_bytes <= 0
            or self.expiry_seconds <= 0
            or self.uploads_per_ip_per_hour <= 0
        ):
            raise ValueError("Upload limits, expiry, and rate limits must be positive.")

    @classmethod
    def from_environment(cls) -> Settings:
        """Create settings using documented environment overrides."""
        return cls(
            data_dir=Path(
                os.environ.get(
                    "SHRINKLY_DATA_DIR",
                    str(Path(tempfile.gettempdir()) / "shrinkly-data"),
                )
            ).resolve(),
            max_upload_bytes=int(
                os.environ.get("SHRINKLY_MAX_UPLOAD_BYTES", 1024 * 1024 * 1024)
            ),
            chunk_bytes=int(
                os.environ.get("SHRINKLY_CHUNK_BYTES", 8 * 1024 * 1024)
            ),
            expiry_seconds=int(
                os.environ.get("SHRINKLY_EXPIRY_SECONDS", 60 * 60)
            ),
            uploads_per_ip_per_hour=int(
                os.environ.get("SHRINKLY_UPLOADS_PER_IP_PER_HOUR", 20)
            ),
        )
