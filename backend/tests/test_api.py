"""HTTP API tests for upload, processing, and cleanup behavior."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pymupdf as fitz
from fastapi.testclient import TestClient

from app.settings import Settings
from app.main import create_app


def _make_pdf() -> bytes:
    """Return a small searchable PDF."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A test document with searchable text.")
    output = document.tobytes()
    document.close()
    return output


def _upload(client: TestClient, content: bytes, filename: str = "sample.pdf") -> str:
    response = client.post(
        "/api/upload/init",
        json={"filename": filename, "file_size": len(content)},
    )
    assert response.status_code == 200
    upload = response.json()
    for chunk_index, start in enumerate(range(0, len(content), upload["chunk_size"])):
        chunk = content[start : start + upload["chunk_size"]]
        result = client.put(
            f"/api/upload/{upload['upload_id']}/chunk?chunk_index={chunk_index}",
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert result.status_code == 200
    return upload["upload_id"]


def test_upload_job_status_and_download(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        max_upload_bytes=1024 * 1024,
        chunk_bytes=1024 * 1024,
    )
    with TestClient(create_app(settings)) as client:
        upload_id = _upload(client, _make_pdf())
        create = client.post(
            "/api/jobs",
            json={
                "upload_id": upload_id,
                "mode": "split",
                "target_ai": "custom",
                "custom_token_limit": 128,
            },
        )
        assert create.status_code == 202
        job_id = create.json()["job_id"]

        status = client.get(f"/api/jobs/{job_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "completed"
        assert status.json()["analysis"]["page_count"] == 1

        download = client.get(f"/api/jobs/{job_id}/download")
        assert download.status_code == 200
        archive_path = tmp_path / "download.zip"
        archive_path.write_bytes(download.content)
        with zipfile.ZipFile(archive_path) as archive:
            assert {"full.md", "manifest.json", "parts/part-01.md"} <= set(
                archive.namelist()
            )
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["format"] == "pdf"

        deleted = client.delete(f"/api/jobs/{job_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_upload_requires_pdf_magic_bytes(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        max_upload_bytes=1024,
        chunk_bytes=1024,
    )
    with TestClient(create_app(settings)) as client:
        upload_id = _upload(client, b"not a PDF", filename="fake.pdf")
        response = client.post(
            "/api/jobs",
            json={
                "upload_id": upload_id,
                "mode": "text_only",
                "target_ai": "chatgpt",
            },
        )
        assert response.status_code == 415
        assert "contents do not match" in response.json()["detail"]


def test_compressed_mode_and_invalid_options_keep_upload_retryable(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        max_upload_bytes=1024 * 1024,
        chunk_bytes=1024 * 1024,
    )
    with TestClient(create_app(settings)) as client:
        upload_id = _upload(client, _make_pdf())
        invalid = client.post(
            "/api/jobs",
            json={"upload_id": upload_id, "target_ai": "custom"},
        )
        assert invalid.status_code == 422

        created = client.post(
            "/api/jobs",
            json={
                "upload_id": upload_id,
                "mode": "compressed_original",
                "target_ai": "chatgpt",
            },
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        status = client.get(f"/api/jobs/{job_id}")
        assert status.json()["status"] == "completed"

        download = client.get(f"/api/jobs/{job_id}/download")
        archive_path = tmp_path / "compressed-result.zip"
        archive_path.write_bytes(download.content)
        with zipfile.ZipFile(archive_path) as archive:
            assert "compressed.pdf" in archive.namelist()
            with fitz.open(stream=archive.read("compressed.pdf"), filetype="pdf") as pdf:
                assert pdf.page_count == 1


def test_upload_size_limit(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        max_upload_bytes=10,
        chunk_bytes=8,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/upload/init",
            json={"filename": "large.pdf", "file_size": 11},
        )
    assert response.status_code == 413
