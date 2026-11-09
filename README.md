# Shrinkly

Shrinkly turns PDFs into searchable Markdown, manageable parts, and an optional
compressed PDF. This repository contains a Phase 1 MVP; it does not send files
to an AI provider.

## What is included

- FastAPI API with ordered, chunked uploads, a 1 GB default file limit, and
  in-process background jobs.
- PDF type/page analysis and page-wise Markdown extraction. Scanned pages are
  OCRed individually; pages with selectable text are not OCRed.
- Token estimates based on characters divided by four and configurable
  ChatGPT, Claude, Gemini, and custom part-size choices.
- ZIP output with `full.md`, `parts/`, and `manifest.json`; compressed-original
  mode also adds a metadata-stripped, stream-compressed `compressed.pdf`.
- A responsive browser UI served by the FastAPI app.
- Per-IP upload-init rate limiting and hourly cleanup of local temporary files.

Provider token limits in `backend/app/config/ai_limits.json` are conservative
defaults, not current provider guarantees. Verify the limits and the selected
provider's file-upload rules before deployment. The MVP's compressed PDF mode
compresses PDF streams but does not downsample embedded images. Markdown
extraction preserves text blocks, headings inferred from font sizes, and basic
list markers; complex tables, charts, and formulas may lose layout.

## Requirements

- Python 3.11 or newer.
- Tesseract OCR installed on the server for scanned pages, with the selected
  language data installed. For example, the default `eng` needs English data.
- The Python dependencies in `backend/requirements.txt`.

On Windows, install Tesseract separately and ensure its executable is on the
server's `PATH`. OCR language names should match installed Tesseract language
data (for example `eng` or `deu`).

## Run locally

From the repository root in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -m uvicorn app.main:app --app-dir backend --reload
```

Open <http://127.0.0.1:8000>. The API is also documented at
<http://127.0.0.1:8000/docs>.

## Configuration

| Environment variable | Default | Description |
| --- | --- | --- |
| `SHRINKLY_DATA_DIR` | System temp directory + `shrinkly-data` | Temporary upload and job storage |
| `SHRINKLY_MAX_UPLOAD_BYTES` | `1073741824` | Maximum input size (1 GiB) |
| `SHRINKLY_CHUNK_BYTES` | `8388608` | Maximum upload chunk size (8 MiB) |
| `SHRINKLY_EXPIRY_SECONDS` | `3600` | Inactivity lifetime for files and completed jobs |
| `SHRINKLY_UPLOADS_PER_IP_PER_HOUR` | `20` | Upload-init requests per client IP per hour |

Use a private, access-controlled data directory with enough disk space. The
default in-memory job registry is for a single-process MVP only: do not run
multiple server workers or expect jobs to survive a restart. Production
deployment should use durable shared job/storage infrastructure, a reverse
proxy request-size limit, HTTPS, and a process/container sandbox with CPU,
memory, and execution-time limits.

The in-process rate limiter is not a substitute for edge rate limiting, and
proxy deployments must configure trusted client IP handling before relying on
per-IP limits. The MVP only accepts PDFs; archive expansion is not supported.

## Tests

From the repository root:

```powershell
python -m pytest backend\tests
```

OCR integration depends on the system Tesseract installation; the automated
tests cover the text-PDF pipeline and confirm that scanned pages are detected.
