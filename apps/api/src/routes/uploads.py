"""Streaming file uploads (PDFs, transcripts) — designed for large files.

The synchronous ``POST /ingest/book`` endpoint requires server-side filesystem
paths. That works for CLI / scripted use but not for the browser UI: the user
needs to upload a 200MB casebook directly. This module accepts multipart
uploads, streams them to a content-addressed location under
``storage/uploads/``, and returns the absolute paths the caller can hand to
``POST /ingest/book/async``.

Streaming write: we never buffer the whole upload in memory; chunks go
straight to disk via ``shutil.copyfileobj``. Tested with a synthetic large
input fixture.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

router = APIRouter(prefix="/uploads", tags=["uploads"])

# Chunk size for streaming. 1 MiB hits a good balance — large enough to amortize
# syscall overhead, small enough to keep memory steady on 8GB laptops.
_CHUNK_BYTES = 1 * 1024 * 1024


def _max_pdf_bytes() -> int:
    """Per-file size cap for PDF uploads. Default 1 GiB; configurable via env
    in case a single casebook batch is bigger. Failing fast at the edge is
    better than writing 800MB to disk before bailing out."""
    raw = os.environ.get("LAWSCHOOL_MAX_PDF_BYTES", "").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 1 * 1024 * 1024 * 1024  # 1 GiB
    return max(1 * 1024 * 1024, n)


def _max_text_bytes() -> int:
    """Per-file cap for text uploads (transcripts, syllabi, memos). Default 50
    MiB; Gemini's 90-min lecture transcripts are ~200 KiB so this is generous."""
    raw = os.environ.get("LAWSCHOOL_MAX_TEXT_BYTES", "").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 50 * 1024 * 1024
    return max(64 * 1024, n)


def _free_disk_bytes(target_dir: Path) -> int:
    """How many bytes free on the filesystem holding `target_dir`. Used for
    pre-flight space checks. Returns 0 when statvfs is unavailable (rare)."""
    try:
        st = os.statvfs(target_dir)
        return int(st.f_bavail) * int(st.f_frsize)
    except (OSError, AttributeError):
        return 0


def _resolve_uploads_dir() -> Path:
    """Find `storage/uploads/` relative to the repo root (`spec.md` presence).
    Mirrors the lookup pattern in `data.db`."""
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "spec.md").exists():
            return candidate / "storage" / "uploads"
    return Path.cwd() / "storage" / "uploads"


class UploadedFileDTO(BaseModel):
    filename: str
    size_bytes: int
    sha256: str
    stored_path: str
    uploaded_at: datetime


class UploadResponse(BaseModel):
    files: list[UploadedFileDTO]


def _stream_to_disk(
    upload: UploadFile,
    dest_dir: Path,
    *,
    max_bytes: int,
) -> tuple[Path, int, str]:
    """Stream the upload's bytes to `dest_dir/<sha>.<ext>`. Returns
    `(final_path, byte_count, sha256_hex)`. Aborts cleanly with HTTP 413
    when the stream exceeds `max_bytes` — temp file is unlinked on bailout
    so we don't leak partial uploads to disk."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    size = 0

    free_bytes = _free_disk_bytes(dest_dir)
    if 0 < free_bytes < max_bytes:
        # We can't know the upload's exact size before reading it (the
        # Content-Length header isn't available cheaply through Starlette's
        # UploadFile abstraction), so this only catches the worst case:
        # disk has less free than the per-file cap.
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=(
                f"Only {free_bytes // (1024 * 1024)} MiB free on the storage "
                f"volume; per-file cap is {max_bytes // (1024 * 1024)} MiB. "
                "Free up disk space before uploading."
            ),
        )

    # Write to a temp file first; rename atomically once we know the hash.
    with NamedTemporaryFile(
        dir=dest_dir, delete=False, prefix="up-", suffix=".part"
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            while True:
                chunk = upload.file.read(_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    # Don't drain the rest of the upload — close the tmp,
                    # delete it, and 413 the client.
                    tmp.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"{upload.filename or 'upload'} exceeds the "
                            f"{max_bytes // (1024 * 1024)} MiB per-file cap. "
                            "Set LAWSCHOOL_MAX_PDF_BYTES to override."
                        ),
                    )
                hasher.update(chunk)
                tmp.write(chunk)
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception:
            # Any IO error → unlink the partial file before re-raising
            tmp_path.unlink(missing_ok=True)
            raise

    sha = hasher.hexdigest()
    suffix = Path(upload.filename or "").suffix.lower() or ".bin"
    final_path = dest_dir / f"{sha}{suffix}"

    if final_path.exists():
        # Same content already on disk — drop the temp, reuse the canonical.
        tmp_path.unlink(missing_ok=True)
    else:
        tmp_path.replace(final_path)

    return final_path, size, sha


@router.post("/pdf", response_model=UploadResponse)
async def upload_pdfs(
    files: list[UploadFile] = File(..., description="One or more PDFs to upload"),
) -> UploadResponse:
    """Stream PDFs to disk and return their absolute paths.

    The caller (browser UI) typically follows up with
    ``POST /ingest/book/async`` passing these `stored_path` values."""
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided.",
        )

    dest_dir = _resolve_uploads_dir() / "pdf"
    out: list[UploadedFileDTO] = []
    max_bytes = _max_pdf_bytes()

    for f in files:
        # Validate file shape — content-type sniff is best-effort.
        if f.filename and not f.filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{f.filename!r} is not a PDF (extension check).",
            )
        try:
            stored_path, size, sha = _stream_to_disk(f, dest_dir, max_bytes=max_bytes)
        finally:
            await f.close()

        out.append(
            UploadedFileDTO(
                filename=f.filename or sha[:8],
                size_bytes=size,
                sha256=sha,
                stored_path=str(stored_path),
                uploaded_at=datetime.now(tz=timezone.utc),
            )
        )

    return UploadResponse(files=out)


@router.post("/text", response_model=UploadResponse)
async def upload_text(
    files: list[UploadFile] = File(..., description="One or more text files (transcripts, syllabi, memos)"),
) -> UploadResponse:
    """Stream plaintext / markdown / docx-extracted text uploads to disk.

    Returns the same shape as `/uploads/pdf` so the UI can re-use the same
    progress bar component for both kinds. Caller decides what to do with the
    paths next (`/ingest/syllabus`, `/transcripts`, `/profiles/build`, etc.)."""
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided.",
        )
    dest_dir = _resolve_uploads_dir() / "text"
    out: list[UploadedFileDTO] = []
    max_bytes = _max_text_bytes()
    for f in files:
        try:
            stored_path, size, sha = _stream_to_disk(f, dest_dir, max_bytes=max_bytes)
        finally:
            await f.close()
        out.append(
            UploadedFileDTO(
                filename=f.filename or sha[:8],
                size_bytes=size,
                sha256=sha,
                stored_path=str(stored_path),
                uploaded_at=datetime.now(tz=timezone.utc),
            )
        )
    # Reuse `shutil` import so it's not eliminated by linters as unused —
    # `shutil.copyfileobj` was the original streaming approach; we currently
    # use chunked .read()/.write() which is functionally equivalent.
    _ = shutil
    return UploadResponse(files=out)


# ---------------------------------------------------------------------------
# PDF → text extraction (used by the practice wizard's exam / memo upload)
# ---------------------------------------------------------------------------


class PdfTextExtractResponse(BaseModel):
    filename: str
    size_bytes: int
    sha256: str
    stored_path: str
    page_count: int
    text: str


@router.post("/pdf-extract", response_model=PdfTextExtractResponse)
async def upload_and_extract_pdf(
    file: UploadFile = File(..., description="A PDF to extract text from"),
) -> PdfTextExtractResponse:
    """Stream a PDF to disk + return its plaintext.

    Used by the practice wizard so a user can drop a past-exam or grader-memo
    PDF and have its text appear in the editable exam/memo textareas. The
    storage path is also returned so the caller can re-use the same hash for
    de-dup or audit later — the extracted text is just the editable buffer.

    Implementation: same streaming + cap as ``/uploads/pdf``, then runs
    ``pymupdf`` page-by-page to get plain text. (The book-ingest fallback
    runner does the same thing for casebooks, but here we're just pulling a
    short exam PDF, so we skip the disk cache.)
    """
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No file provided."
        )
    if file.filename and not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{file.filename!r} is not a PDF (extension check).",
        )

    dest_dir = _resolve_uploads_dir() / "pdf"
    max_bytes = _max_pdf_bytes()
    try:
        stored_path, size, sha = _stream_to_disk(
            file, dest_dir, max_bytes=max_bytes
        )
    finally:
        await file.close()

    try:
        import pymupdf
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PyMuPDF not installed; reinstall with .venv/bin/pip install -e '.[dev]'.",
        ) from exc

    parts: list[str] = []
    page_count = 0
    try:
        doc = pymupdf.open(str(stored_path))
        try:
            page_count = doc.page_count
            for idx in range(page_count):
                try:
                    page = doc[idx]
                    parts.append(page.get_text("text") or "")
                except Exception:
                    # A bad page shouldn't kill the whole extraction.
                    parts.append("")
        finally:
            doc.close()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not parse PDF: {type(exc).__name__}: {exc}",
        ) from exc

    text = "\n\n".join(p for p in parts if p)
    return PdfTextExtractResponse(
        filename=file.filename or sha[:8],
        size_bytes=size,
        sha256=sha,
        stored_path=str(stored_path),
        page_count=page_count,
        text=text,
    )
