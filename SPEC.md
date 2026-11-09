# Shrinkly

> Working name. Check domain and trademark availability before launch.

**Launch tagline:** Make any PDF AI-ready.
**Long-term tagline:** Make any file AI-ready.

---

# PART A: HOW TO USE THIS FILE WITH COPILOT

1. Save this file as `SPEC.md` in the repo root.
2. Copy the block in **Part B** into `.github/copilot-instructions.md`. Copilot reads it automatically (check GitHub's current docs, since features change).
3. Start every session with: **"Read SPEC.md first."**
4. Work through **Part D (build order)** one task at a time, using the prompt template in **Part C**.

---

# PART B: COPILOT INSTRUCTIONS

Copy everything in this block into `.github/copilot-instructions.md`.

````markdown
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
````

---

# PART C: PROMPT TEMPLATE

```
Context: Shrinkly backend, see SPEC.md section <number>.
Task: <one small, specific task>
Files: <files to create or edit>
Input/Output: <what goes in, what comes out>
Constraints: <libraries, versions, things to avoid>
Done when: <tests pass / measurable result>
```

**Example**
```
Context: Shrinkly backend, see SPEC.md section 5a.
Task: Implement analyze() in handlers/pdf.py.
Files: app/handlers/pdf.py, tests/test_pdf.py
Input/Output: PDF path in, AnalysisResult out (page count, per-page text vs scanned).
Constraints: Python 3.11, PyMuPDF only, no network calls, stream pages.
Done when: unit tests pass for a text PDF, a scanned PDF and a mixed PDF.
```

**Prompting tips**
- One small task per prompt. Attach the files you mention.
- Ask for a plan first, then the code. Ask for tests first when possible.
- Review every change and run the code. Commit to git after each working step.
- When something breaks, paste the exact error.
- Start a fresh chat if Copilot drifts or repeats itself.
- Never put API keys or secrets in prompts or code.

---

# PART D: BUILD ORDER (give these to Copilot one at a time)

1. Scaffold the FastAPI app, the `FormatHandler` base class and `registry.py`
   (detect by magic bytes, then extension).
2. Implement `handlers/pdf.py` `analyze()` (text vs scanned pages) with tests.
3. Implement PDF `to_markdown()` with OCR for scanned pages only.
4. Implement `services/tokens.py` and `services/split.py` (split at headings, respect token limit).
5. Implement `services/package.py` (ZIP with `full.md`, `parts/`, `manifest.json`).
6. Add chunked upload and the job endpoints (Section 8).
7. Add the 1-hour cleanup task.
8. Build the frontend: upload, analysis summary, mode picker, progress, download.
9. Raise the file limit from 200 MB to 1 GB and test performance.
10. Only after the MVP works: DOCX and PPTX handlers.

---

# PART E: PRODUCT SPEC

## 1. Goal

A tool inside my existing file-conversion site (Convertly) that takes a large or messy file and returns a lightweight, AI-friendly version that fits LLM upload limits and is easy for an LLM to understand.

- **Phase 1 (launch):** PDF only, up to 1 GB.
- **Later phases:** DOCX, PPTX, XLSX/CSV, images, audio/video, mixed ZIPs.

## 2. Problem

- Large files (often scans or image-heavy PDFs) exceed AI chat upload limits.
- Scanned files contain no real text, so LLMs read them poorly.
- Spreadsheets, slide decks and Word files carry formatting noise that wastes tokens.
- Users don't know why their file fails or how to fix it.

## 3. Users

Students, researchers, lawyers, analysts, and anyone who uploads documents to ChatGPT, Claude or Gemini.

## 4. Core flow (same for every format)

1. User uploads a file (drag and drop, chunked/resumable upload).
2. System analyzes it: type, size, page/sheet/slide count, text vs scanned, estimated tokens.
3. System shows a summary, e.g. "Your file is 1 GB, 600 pages, scanned. Estimated AI-ready size: ~8 MB."
4. User picks a mode:
   - **Text only (Markdown):** smallest, best for LLMs.
   - **Compressed original:** keeps layout, images downsampled (where supported).
   - **Split into parts:** chunks sized for a chosen AI tool.
5. System processes the file in a background job and shows progress.
6. User downloads a ZIP.

## 5. Architecture: one pipeline, many format handlers

```python
class FormatHandler:
    extensions: list[str]
    mime_types: list[str]
    def analyze(self, path) -> AnalysisResult: ...
    def to_markdown(self, path, options) -> MarkdownResult: ...
    def compress(self, path, options) -> Path | None: ...
```

### 5a. Phase 1 handler: PDF

1. Detect scanned vs text pages (check extractable text per page).
2. If scanned: run OCR (OCRmyPDF + Tesseract), language selectable, default English. OCR only the scanned pages in mixed files.
3. Extract to Markdown, keeping headings, lists and tables (pymupdf4llm or PyMuPDF).
4. Optional: compress images and strip metadata (pikepdf or Ghostscript).

### 5b. Shared steps (all formats)

1. Estimate token count (approx. characters / 4).
2. Split at heading/section boundaries into parts under a target size. Presets: ChatGPT, Claude, Gemini, or custom token/MB limit. Check each provider's current limits at build time and keep them in a config file.
3. Add a header to each part: "Part 3 of 12, pages 81-110".
4. Build the output ZIP:
   - `full.md`
   - `parts/part-01.md` ... `part-NN.md`
   - `compressed.<ext>` (if chosen)
   - `manifest.json` (source file, ranges, token estimates, warnings)

## 6. Format roadmap

| Phase | Format | Approach | Suggested libraries |
|---|---|---|---|
| 1 | PDF | Analyze, OCR if needed, Markdown, split | PyMuPDF, pymupdf4llm, OCRmyPDF, pikepdf |
| 2 | DOCX | Markdown with headings, lists, tables; drop or list embedded images | python-docx, mammoth, or pandoc |
| 2 | PPTX | One section per slide; titles, text, speaker notes | python-pptx |
| 3 | XLSX / CSV | Trim empty rows/columns; one table per sheet; summarize wide sheets; split by rows | openpyxl, pandas |
| 3 | Images (JPG, PNG, HEIC, WebP, TIFF) | OCR to text; optional downscale | Tesseract, Pillow, pillow-heif |
| 4 | Audio / video | Transcribe to text with timestamps | A speech-to-text engine (e.g. Whisper) |
| 4 | ZIP of mixed files | Run each file through its handler; one bundle with an index | handler registry |

## 7. Tech stack

- Backend: Python 3.11+, FastAPI
- Jobs: BackgroundTasks for MVP; RQ or Celery with Redis later
- Frontend: simple HTML/JS or React, matching the existing Convertly UI
- Storage: local temp disk for MVP, S3-compatible bucket later
- Upload: chunked or resumable (tus protocol or custom chunking)

## 8. API endpoints

- `POST /api/upload/init` → returns `upload_id`
- `PUT /api/upload/{upload_id}/chunk` → uploads a chunk
- `POST /api/jobs` → body: `{upload_id, mode, target_ai, ocr_language}`
- `GET /api/jobs/{job_id}` → status, progress %, detected format, warnings
- `GET /api/jobs/{job_id}/download` → ZIP
- `DELETE /api/jobs/{job_id}` → delete files immediately

## 9. Privacy and safety (required)

- Auto-delete uploads and outputs after 1 hour.
- No file content in logs.
- Rate-limit per IP; enforce max file size per format; reject unsupported types by magic bytes.
- Process files in a sandboxed temp directory.
- Zip-bomb protection (size and file-count limits on archives).
- Clear notice: "Files are deleted automatically."

## 10. Edge cases

- Password-protected files: ask for the password or fail clearly.
- Corrupted files: friendly error.
- Mixed text/scanned pages: OCR only scanned pages.
- Tables, formulas and charts may degrade: warn in `manifest.json`.
- Low OCR confidence: flag those pages.
- Very wide spreadsheets: summarize columns and warn about truncation.
- Unknown format: list supported formats in the error.

## 11. MVP scope

1. PDF upload (start at 200 MB, then raise to 1 GB)
2. Detect scanned vs text
3. Extract to Markdown (OCR when needed)
4. Split into parts by token limit
5. ZIP download
6. Auto-delete after 1 hour
7. Handler registry in place so later formats plug in cleanly

## 12. Later versions

- Compressed-original mode for more formats
- Telegram bot: send a file, receive the ZIP
- Per-part auto-summaries using an LLM API
- Public API with free and paid tiers
- SEO landing pages ("PDF too large for ChatGPT", "compress PDF for Claude")
- Change the tagline to "Make any file AI-ready" once Phase 3 ships

## 13. Acceptance criteria

- A 500-page scanned PDF becomes Markdown under 10 MB.
- Every part is under the selected token limit.
- Headings and basic tables are preserved in the Markdown.
- Progress is visible during processing.
- Files are deleted after 1 hour.
- Works on desktop and mobile browsers.
- Adding a new format needs only a new handler and a registry entry, with no API changes.

## 14. Folder structure

```
shrinkly/
  .github/copilot-instructions.md
  backend/
    app/main.py
    app/routes/
    app/handlers/{base,pdf,docx,pptx,xlsx,image,media,archive}.py
    app/registry.py
    app/services/{analyze,ocr,split,tokens,package,cleanup}.py
    app/workers/
    app/config/ai_limits.json
    tests/
  frontend/
  SPEC.md
  README.md
```
