# Copilot instructions for Shrinkly

## Project
Shrinkly turns large or messy files into small, clean, AI-friendly files
(Markdown plus split parts). Phase 1 is PDF only. Read SPEC.md for the full design.

## Stack
- Python 3.11+, FastAPI, pytest
- PDF/OCR: PyMuPDF, pymupdf4llm, OCRmyPDF, Tesseract, pikepdf
- Jobs: FastAPI BackgroundTasks for MVP, RQ/Celery + Redis later
- Frontend: simple HTML/JS matching the existing Convertly style

## Architecture rules
- Every format is a handler class implementing `FormatHandler`
  (`analyze`, `to_markdown`, `compress`) in `app/handlers/`.
- Handlers are chosen in `app/registry.py` by magic bytes first, then extension.
- Shared logic (token estimate, split, package, cleanup) lives in `app/services/`
  and must not depend on any one format.
- The API is format-agnostic. Never add format-specific endpoints.
- AI provider limits live in `app/config/ai_limits.json`, never hard-coded.

## Coding rules
- Type hints on all functions. Short functions with docstrings.
- Write or update tests for every change. Run pytest before finishing.
- Process large files in streams or per page. Never load a whole 1 GB file into memory.
- Use pathlib. Use temp directories that are cleaned up on success and failure.
- Handle errors explicitly with clear user-facing messages.

## Security and privacy rules
- Never log file contents or file names containing user data.
- Validate uploads by magic bytes, not extension. Enforce size limits.
- Limit archive size and file count (zip-bomb protection).
- Auto-delete uploads and outputs after 1 hour.
- Never hard-code secrets or API keys. Read them from environment variables.

## Do not
- Do not add dependencies without saying why.
- Do not change files outside the task scope.
- Do not rewrite working code unless asked.
- Do not make network calls from processing code unless the task says so.

## How to work
- For any task touching more than one file, list a short plan first.
- Make small changes. Explain what changed and how to test it.
