# Shrinkly

Shrinkly turns PDFs into searchable Markdown, manageable parts, and an optional
compressed PDF. This repository contains a Phase 1 MVP; it does not send files
to an AI provider.

## What is included

- FastAPI API with ordered, chunked uploads, a 1 GB default file limit, and
  local background jobs for development, plus an optional Redis/RQ worker mode.
- PDF type/page analysis and page-wise Markdown extraction. Scanned pages are
  OCRed individually; pages with selectable text are not OCRed.
- Token estimates based on characters divided by four and configurable
  ChatGPT, Claude, Gemini, and custom part-size choices.
- Three explicit output modes: one `.md` download, a parts-only ZIP with a
  manifest, or one metadata-stripped, stream-compressed `.pdf` download.
- A responsive browser UI served by the FastAPI app, with a persistent
  light/dark theme toggle, contact link, and UI version badge.
- Per-IP upload-init rate limiting and hourly cleanup of local temporary files.
- Docker Compose deployment with Redis, a resource-limited worker, Tesseract,
  shared storage, and an Nginx upload/rate-limit layer.

Provider token limits in `backend/app/config/ai_limits.json` are conservative
defaults, not current provider guarantees. Verify the limits and the selected
provider's file-upload rules before deployment. Split mode packages only
`parts/part-*.md` and `manifest.json`; it intentionally omits the duplicate
full Markdown document. The compressed PDF mode returns only the PDF, compresses
PDF streams, and downsamples images whose effective resolution exceeds 300 DPI
toward 150 DPI at JPEG quality 75; this is lossy and may reduce image quality.
Markdown extraction preserves text blocks, headings inferred from font sizes,
basic list markers, and tables that PyMuPDF detects; complex layouts, charts,
and formulas may lose structure. Vector-only or already-low-resolution PDFs
may not become smaller.

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

## Run the Redis worker stack with Docker

Install Docker Desktop, then start the included API, Redis, RQ worker, and Nginx
services from the repository root:

```powershell
docker compose up --build
```

Open <http://localhost:8080>. This demo stack sets a 50 MiB upload cap, limits
the worker to 2 GiB RAM and 1.5 CPUs, and terminates any individual job that
runs longer than 15 minutes. It stores upload data and Redis's AOF in Docker
named volumes. The Nginx service limits chunk request bodies to 9 MiB and
applies per-IP API request limits.

The Compose proxy listens on HTTP for local use. For public deployment, put it
behind a trusted HTTPS-terminating edge/proxy, restrict network access so the
API and worker are not directly exposed, and configure the edge upload and
rate limits too. Set `SHRINKLY_TRUST_PROXY_HEADERS=true` only when the API is
reachable exclusively through a proxy that overwrites `X-Forwarded-For`.

The web process persists upload/job metadata in Redis when
`SHRINKLY_REDIS_URL` is set and queues work to RQ. The worker must share both
the Redis service and the same mounted `SHRINKLY_DATA_DIR`. Do not run Redis
mode on Render Free: it does not provide the persistent shared disk and
always-on worker this setup requires. For Render, use paid services with shared
durable storage and a separately provisioned Redis service, or use Render Free
only for small, disposable demos with the in-process mode.

## Configuration

| Environment variable | Default | Description |
| --- | --- | --- |
| `SHRINKLY_DATA_DIR` | System temp directory + `shrinkly-data` | Temporary upload and job storage |
| `SHRINKLY_MAX_UPLOAD_BYTES` | `1073741824` | Maximum input size (1 GiB) |
| `SHRINKLY_CHUNK_BYTES` | `8388608` | Maximum upload chunk size (8 MiB) |
| `SHRINKLY_EXPIRY_SECONDS` | `3600` | Inactivity lifetime for files and completed jobs |
| `SHRINKLY_UPLOADS_PER_IP_PER_HOUR` | `20` | Upload-init requests per client IP per hour |
| `SHRINKLY_REDIS_URL` | Unset | Enable Redis-backed job/upload metadata and RQ queue mode |
| `SHRINKLY_JOB_TIMEOUT_SECONDS` | `900` | Maximum RQ execution time for one PDF job |
| `SHRINKLY_TRUST_PROXY_HEADERS` | `false` | Read client IP from `X-Forwarded-For` only behind a trusted proxy |

Use a private, access-controlled data directory with enough disk space. Without
Redis configured, the fallback job registry is in process memory and is only
appropriate for local development. Redis preserves metadata and queued jobs
across web restarts, while the actual uploaded PDFs and outputs still require
shared durable filesystem storage accessible at the same path to web and worker.
The RQ worker enforces a per-job time limit; Docker Compose additionally applies
CPU, memory, process-count, read-only-root-filesystem, and no-new-privileges
limits to API and worker containers.

The in-process rate limiter is not a substitute for edge rate limiting, and
proxy deployments must configure trusted client IP handling before relying on
per-IP limits. The MVP only accepts PDFs; archive expansion is not supported.

## Tests

From the repository root:

```powershell
python -m pip install -r backend\requirements-dev.txt
python -m pytest backend\tests
```

OCR integration depends on the system Tesseract installation; the automated
tests cover the text-PDF pipeline, password-protected and corrupt files,
high-DPI image downsampling, and a 500-page mixed document with OCR mocked for
the scanned page. To test real OCR, run the Docker stack or install Tesseract
and its language packs locally.

## UI version

The footer displays the current UI version. Update the version badge in
`frontend/index.html` for each feature commit using the project's manual
versioning convention (for example, `v2.1.1`, then `v2.1.2`).
