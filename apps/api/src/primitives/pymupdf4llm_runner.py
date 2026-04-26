"""PyMuPDF runner — the spec §4.1.1 fallback for environments where
``marker-pdf`` can't be installed.

Marker is the production-quality engine and remains the default. We named
this module after PyMuPDF4LLM (the spec's named fallback) and originally used
that library, but per-page calls turned out to do O(N²) global layout
analysis — a 1500-page casebook needed ~24 hours. We now use the lower-level
``pymupdf`` library directly (which both ``marker`` and ``pymupdf4llm``
depend on) — `page.get_text("text")` is microseconds per page and gives us
plain text that the downstream block segmenter handles fine. The full module
name stays for compatibility with existing callers / tests.

Output shape matches :class:`primitives.marker_runner.MarkerResult` exactly so
the rest of the ingest pipeline (page-marker extraction, block segmentation,
TOC extraction) is unaware of which engine produced the markdown.

Trade-offs vs. Marker:
- No LLM polish, no table inference, no heading detection. Plain text only.
- Source-page markers still work: PyMuPDF preserves bare-numeric lines in
  page headers/footers exactly as they appear, and the existing extractor
  finds them by regex.
- Block segmentation downstream uses regex patterns (case-name capitals,
  citation patterns, "1." numbered notes). Those work on plain text.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import structlog

from primitives.marker_runner import MarkerResult, _resolve_default_cache_dir

# Progress callback shape: ``(pages_done, total_pages) -> None``. Used by the
# ingest pipeline to surface fine-grained progress. With raw pymupdf this is
# barely necessary (the whole pipeline finishes in seconds) but we keep it
# for the multi-megabyte case where even fast extraction is worth a progress
# bar.
PageProgressCallback = Callable[[int, int], None]

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Cache directory — reused from marker_runner so the two engines don't fight
# over the same {hash}.md file. We namespace via a sibling subdir.
# ---------------------------------------------------------------------------


def _resolve_cache_dir() -> Path:
    """Mirror ``marker_runner._resolve_default_cache_dir`` shape but namespace
    the cache so a re-ingest with Marker doesn't pick up a fallback file."""
    override = os.environ.get("LAWSCHOOL_PYMUPDF_CACHE_DIR")
    if override:
        return Path(override)
    return _resolve_default_cache_dir().parent / "pymupdf_raw"


# ---------------------------------------------------------------------------
# Hashing — content-addressed cache key, matches marker_runner behaviour
# ---------------------------------------------------------------------------


def _hash_pdf(pdf_path: Path) -> str:
    h = hashlib.sha256()
    with pdf_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run_pymupdf4llm_cached(
    pdf_path: Path,
    *,
    on_page: PageProgressCallback | None = None,
) -> MarkerResult:
    """Convert ``pdf_path`` to plain-text markdown via raw ``pymupdf``, cached
    on disk.

    Cache layout (mirrors marker_runner): ``{cache_dir}/{sha}.md`` plus
    ``{sha}.meta.json`` carrying ``{pdf_page_count, pdf_page_offsets}`` so we
    don't have to re-extract on subsequent ingests of the same PDF.

    ``on_page`` (optional) is invoked as ``on_page(pages_done, total_pages)``
    after each page is rendered. Throttled internally to at most one event
    per 100 ms so the task DB doesn't get hammered.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"pymupdf4llm: {pdf_path} does not exist")

    cache_dir = _resolve_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    sha = _hash_pdf(pdf_path)
    md_cache = cache_dir / f"{sha}.md"
    meta_cache = cache_dir / f"{sha}.meta.json"

    if md_cache.exists() and meta_cache.exists():
        meta = json.loads(meta_cache.read_text(encoding="utf-8"))
        if on_page is not None:
            count = int(meta["pdf_page_count"])
            try:
                on_page(count, count)
            except Exception:  # progress is advisory; never break ingestion
                pass
        return MarkerResult(
            markdown=md_cache.read_text(encoding="utf-8"),
            pdf_page_count=int(meta["pdf_page_count"]),
            pdf_page_offsets=[int(x) for x in meta["pdf_page_offsets"]],
        )

    log.info("pymupdf_extract_start", pdf=str(pdf_path), sha=sha[:12])
    started = time.monotonic()
    result = _extract(pdf_path, on_page=on_page)
    duration_s = time.monotonic() - started

    md_cache.write_text(result.markdown, encoding="utf-8")
    meta_cache.write_text(
        json.dumps(
            {
                "pdf_page_count": result.pdf_page_count,
                "pdf_page_offsets": result.pdf_page_offsets,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info(
        "pymupdf_extract_done",
        pdf=str(pdf_path),
        sha=sha[:12],
        page_count=result.pdf_page_count,
        markdown_chars=len(result.markdown),
        duration_s=round(duration_s, 2),
    )
    return result


def _extract(
    pdf_path: Path, *, on_page: PageProgressCallback | None = None
) -> MarkerResult:
    """Extract per-page text with raw ``pymupdf`` and stitch into a MarkerResult.

    Uses ``page.get_text("text")`` — pure C path through MuPDF. On a 1500-page
    casebook this runs in 5–15 seconds (vs. ~24 hours for the prior
    pymupdf4llm-per-page approach, which re-ran document-wide layout analysis
    on every call).

    Wraps import failures into ``MarkerNotInstalledError`` so callers that
    already expect that exception (the ingest pipeline) keep working when
    pymupdf itself is missing — extremely unusual since it's a transitive
    dependency of half the stack, but worth surfacing 503 with an install hint
    rather than a 500.
    """
    try:
        import pymupdf
    except ImportError as exc:
        from primitives.marker_runner import MarkerNotInstalledError

        raise MarkerNotInstalledError(
            "PyMuPDF fallback unavailable: "
            ".venv/bin/pip install 'pymupdf>=1.24'"
        ) from exc

    def _safe_emit(done: int, total: int, *, state: list[float]) -> None:
        if on_page is None:
            return
        now = time.monotonic()
        # Always emit first/last; throttle middle calls to ~100 ms so a 2000-
        # page book emits ~50 progress events instead of 2000.
        if done > 0 and done < total and now - state[0] < 0.1:
            return
        state[0] = now
        try:
            on_page(done, total)
        except Exception:  # progress is advisory; never break ingestion
            log.warning("pymupdf_progress_callback_error", exc_info=True)

    doc = pymupdf.open(str(pdf_path))
    try:
        page_count = doc.page_count
        last_emit_state = [0.0]
        _safe_emit(0, page_count, state=last_emit_state)

        parts: list[str] = []
        offsets: list[int] = []
        cursor = 0
        for idx in range(page_count):
            try:
                page = doc[idx]
                page_text = page.get_text("text") or ""
            except Exception as exc:
                # A single bad page shouldn't kill the whole ingest.
                log.warning(
                    "pymupdf_page_failed",
                    pdf=str(pdf_path),
                    page_index=idx,
                    error=str(exc),
                )
                page_text = ""

            offsets.append(cursor)
            if idx > 0:
                parts.append("\n\n")
                cursor += 2
            parts.append(page_text)
            cursor += len(page_text)

            _safe_emit(idx + 1, page_count, state=last_emit_state)

        offsets.append(cursor)
        markdown = "".join(parts)
        return MarkerResult(
            markdown=markdown,
            pdf_page_count=page_count,
            pdf_page_offsets=offsets,
        )
    finally:
        doc.close()


__all__ = ["run_pymupdf4llm_cached"]
