"""Thin wrapper around Marker (spec §4.1.1 step 2).

Marker is the PDF→markdown extractor. The Python API is imported lazily so that
(a) tests don't require `marker-pdf` to be installed — they patch
`_run_marker_impl` directly — and (b) users who don't run ingestion (e.g., they
only use an already-ingested book) never pay the import cost.

Caching: spec §4.1.1 step 2 requires storing raw Marker output under
`storage/marker_raw/{hash}.md`. A sibling `{hash}.meta.json` captures the
pdf-page-to-char-offset mapping so step 5 of the ingestion pipeline can
reconstruct `pdf_page_start`/`pdf_page_end` from the cached markdown without
re-running Marker.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MarkerNotInstalledError(RuntimeError):
    """Raised when Marker is required but `marker-pdf` is not importable.

    The public surface wraps the underlying `ImportError` into this dedicated
    exception class so FastAPI routes can translate cleanly into an HTTP 503
    with an actionable install instruction.
    """


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerResult:
    """The product of running Marker on a single batch PDF.

    `pdf_page_offsets[i]` is the character offset in `markdown` at which PDF
    page `i` (0-indexed) begins. `pdf_page_offsets[pdf_page_count]` — if
    present — marks the end of the markdown; callers can pass it as the upper
    bound of the last page or ignore it. We maintain this one-extra-entry
    convention where possible, but do NOT require it — code that computes
    pdf-page-end from a char offset should clamp to `len(markdown)`.
    """

    markdown: str
    pdf_page_count: int
    pdf_page_offsets: list[int]


# ---------------------------------------------------------------------------
# Cache-directory resolution
# ---------------------------------------------------------------------------


def _resolve_default_cache_dir() -> Path:
    """Resolve ``storage/marker_raw/``.

    Honors ``LAWSCHOOL_MARKER_CACHE_DIR``. Otherwise routes through
    ``paths.storage_root`` so the bundled .app gets
    ``~/Library/Application Support/cLAWd/marker_raw/`` (writable) instead
    of ``Path.cwd() / storage / marker_raw`` which evaluates to the
    unwritable filesystem root inside the .app bundle.
    """
    override = os.environ.get("LAWSCHOOL_MARKER_CACHE_DIR")
    if override:
        return Path(override)

    from paths import storage_root

    return storage_root() / "marker_raw"


# ---------------------------------------------------------------------------
# Internal impl (mockable)
# ---------------------------------------------------------------------------


def _run_marker_impl(
    pdf_path: Path,
    *,
    use_llm: bool,
    extract_images: bool,
) -> MarkerResult:
    """The actual Marker call. Patched by unit tests via `monkeypatch.setattr`.

    Split out as a module-level function so tests can swap it in without
    shimming the Marker import itself. The default implementation is a thin
    bridge to `marker.converters.pdf.PdfConverter`; if `marker-pdf` is not
    installed the ImportError propagates and the public wrapper catches it.
    """
    # Deferred import — the whole point of this split is that tests shouldn't
    # need marker-pdf installed.
    try:
        from marker.converters.pdf import PdfConverter  # type: ignore[import-not-found]
        from marker.models import create_model_dict  # type: ignore[import-not-found]
        from marker.output import text_from_rendered  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — exercised via the wrapper
        raise ImportError(str(exc)) from exc

    converter = PdfConverter(
        artifact_dict=create_model_dict(),
        config={
            "use_llm": use_llm,
            "extract_images": extract_images,
            "output_format": "markdown",
        },
    )
    rendered = converter(str(pdf_path))
    markdown, _metadata, _images = text_from_rendered(rendered)

    # Try to recover per-page offsets from the rendered object. Marker's
    # current rendering emits a `<!-- page N -->` style comment per PDF page
    # boundary; different versions emit differently-shaped metadata. We look
    # in a few spots, fall back to a single-page assumption if nothing is
    # available.
    pdf_page_count, pdf_page_offsets = _extract_pdf_page_offsets(markdown, rendered)
    return MarkerResult(
        markdown=markdown,
        pdf_page_count=pdf_page_count,
        pdf_page_offsets=pdf_page_offsets,
    )


def _extract_pdf_page_offsets(
    markdown: str, rendered: Any
) -> tuple[int, list[int]]:
    """Best-effort recovery of per-pdf-page char offsets.

    Marker's rendered output varies by version. We check a few candidate
    locations and, if none are present, degrade gracefully to a single page
    spanning the whole markdown. Downstream code treats this as "unknown" and
    cross-validates against extracted source-page markers instead.
    """
    # Attempt 1: some Marker versions attach `metadata["page_offsets"]`.
    metadata = getattr(rendered, "metadata", None)
    if isinstance(metadata, dict):
        offsets = metadata.get("page_offsets")
        if isinstance(offsets, list) and all(isinstance(x, int) for x in offsets):
            count = max(1, len(offsets))
            return count, list(offsets)

    # Attempt 2: scan for `<!-- page N -->` sentinels.
    import re

    marker_re = re.compile(r"<!--\s*page\s+(\d+)\s*-->", re.IGNORECASE)
    offsets = []
    for m in marker_re.finditer(markdown):
        offsets.append(m.start())
    if offsets:
        return len(offsets), offsets

    # Fallback: assume one page.
    return 1, [0]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_marker(
    pdf_path: Path,
    *,
    use_llm: bool = True,
    extract_images: bool = True,
) -> MarkerResult:
    """Invoke Marker on `pdf_path` and return structured output.

    Raises `MarkerNotInstalledError` if `marker-pdf` is not installed — the
    caller can translate this to an HTTP 503 with install instructions.
    """
    try:
        return _run_marker_impl(
            pdf_path,
            use_llm=use_llm,
            extract_images=extract_images,
        )
    except ImportError as exc:
        raise MarkerNotInstalledError(
            "marker-pdf not installed. With pip venv: "
            ".venv/bin/pip install -e '.[dev,marker]'  "
            "(or with uv: uv sync --extra marker)"
        ) from exc


def run_marker_cached(
    pdf_path: Path,
    *,
    cache_dir: Path | None = None,
    use_llm: bool = True,
    extract_images: bool = True,
) -> MarkerResult:
    """Run Marker with on-disk caching keyed by PDF content hash.

    Cache layout under `cache_dir/`:
      `{hash}.md`        — the raw markdown from Marker
      `{hash}.meta.json` — `{pdf_page_count, pdf_page_offsets}` companion

    Hit: both files exist → decode and return without calling Marker.
    Miss: call Marker, write both files atomically, return the result.
    """
    cache = cache_dir if cache_dir is not None else _resolve_default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    content_hash = _hash_file(pdf_path)
    md_path = cache / f"{content_hash}.md"
    meta_path = cache / f"{content_hash}.meta.json"

    if md_path.exists() and meta_path.exists():
        try:
            markdown = md_path.read_text(encoding="utf-8")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            log.info("marker_cache_hit", pdf_path=str(pdf_path), hash=content_hash)
            return MarkerResult(
                markdown=markdown,
                pdf_page_count=int(meta["pdf_page_count"]),
                pdf_page_offsets=list(meta["pdf_page_offsets"]),
            )
        except (OSError, ValueError, KeyError) as exc:
            # Corrupt cache — fall through to a fresh run.
            log.warning(
                "marker_cache_corrupt",
                pdf_path=str(pdf_path),
                hash=content_hash,
                error=str(exc),
            )

    log.info("marker_cache_miss", pdf_path=str(pdf_path), hash=content_hash)
    result = run_marker(
        pdf_path,
        use_llm=use_llm,
        extract_images=extract_images,
    )

    # Write atomically: temp file then rename, so a crashed write doesn't
    # leave a half-written cache entry.
    _atomic_write_text(md_path, result.markdown)
    meta_payload = {
        "pdf_page_count": result.pdf_page_count,
        "pdf_page_offsets": list(result.pdf_page_offsets),
    }
    _atomic_write_text(meta_path, json.dumps(meta_payload))

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    """SHA-256 the file bytes. Streamed in 64 KiB chunks for large PDFs."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


__all__ = [
    "MarkerNotInstalledError",
    "MarkerResult",
    "run_marker",
    "run_marker_cached",
]


# Exported for tests that want to inspect the dataclass shape without
# importing dataclasses.asdict directly.
def _result_as_dict(r: MarkerResult) -> dict[str, Any]:  # pragma: no cover
    return asdict(r)
