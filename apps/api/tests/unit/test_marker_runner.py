"""Unit tests for primitives/marker_runner.py (spec §4.1.1 step 2).

Marker itself is never invoked — tests patch `_run_marker_impl` so the wrapper
surface, caching, and error translation are exercised without the heavy
dependency.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primitives import marker_runner
from primitives.marker_runner import (
    MarkerNotInstalledError,
    MarkerResult,
    run_marker,
    run_marker_cached,
)


def _write_fake_pdf(path: Path, content: bytes = b"%PDF-FAKE\n") -> Path:
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# run_marker
# ---------------------------------------------------------------------------


def test_run_marker_calls_impl_with_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_impl(pdf_path: Path, *, use_llm: bool, extract_images: bool) -> MarkerResult:
        captured["pdf_path"] = pdf_path
        captured["use_llm"] = use_llm
        captured["extract_images"] = extract_images
        return MarkerResult(markdown="ok", pdf_page_count=1, pdf_page_offsets=[0])

    monkeypatch.setattr(marker_runner, "_run_marker_impl", fake_impl)
    p = _write_fake_pdf(tmp_path / "b.pdf")

    result = run_marker(p, use_llm=False, extract_images=True)
    assert result.markdown == "ok"
    assert captured["pdf_path"] == p
    assert captured["use_llm"] is False
    assert captured["extract_images"] is True


def test_run_marker_not_installed_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_impl(*_args: object, **_kwargs: object) -> MarkerResult:
        raise ImportError("no module named 'marker'")

    monkeypatch.setattr(marker_runner, "_run_marker_impl", fake_impl)
    p = _write_fake_pdf(tmp_path / "b.pdf")

    with pytest.raises(MarkerNotInstalledError) as exc_info:
        run_marker(p)
    assert "uv sync --extra marker" in str(exc_info.value)


# ---------------------------------------------------------------------------
# run_marker_cached
# ---------------------------------------------------------------------------


def test_run_marker_cached_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    pdf = _write_fake_pdf(tmp_path / "doc.pdf", b"%PDF-HITTEST\n")

    # Pre-populate the cache with what run_marker_cached would have written.
    # We need to compute the same hash the module computes.
    content_hash = marker_runner._hash_file(pdf)
    (cache / f"{content_hash}.md").write_text("# cached\n", encoding="utf-8")
    (cache / f"{content_hash}.meta.json").write_text(
        json.dumps({"pdf_page_count": 2, "pdf_page_offsets": [0, 5]}),
        encoding="utf-8",
    )

    called = {"n": 0}

    def fake_impl(*_args: object, **_kwargs: object) -> MarkerResult:
        called["n"] += 1
        return MarkerResult(markdown="SHOULD NOT BE USED", pdf_page_count=0, pdf_page_offsets=[])

    monkeypatch.setattr(marker_runner, "_run_marker_impl", fake_impl)

    result = run_marker_cached(pdf, cache_dir=cache)
    assert called["n"] == 0
    assert result.markdown == "# cached\n"
    assert result.pdf_page_count == 2
    assert result.pdf_page_offsets == [0, 5]


def test_run_marker_cached_miss_then_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    pdf = _write_fake_pdf(tmp_path / "doc.pdf", b"%PDF-MISSTEST\n")

    impl_call_count = {"n": 0}

    def fake_impl(_pdf: Path, *, use_llm: bool, extract_images: bool) -> MarkerResult:
        impl_call_count["n"] += 1
        return MarkerResult(
            markdown=f"# call {impl_call_count['n']}\n",
            pdf_page_count=1,
            pdf_page_offsets=[0],
        )

    monkeypatch.setattr(marker_runner, "_run_marker_impl", fake_impl)

    first = run_marker_cached(pdf, cache_dir=cache)
    assert impl_call_count["n"] == 1
    assert first.markdown == "# call 1\n"

    # Second call: must hit the cache, NOT invoke impl again.
    second = run_marker_cached(pdf, cache_dir=cache)
    assert impl_call_count["n"] == 1  # unchanged
    assert second.markdown == first.markdown
    assert second.pdf_page_count == first.pdf_page_count
    assert second.pdf_page_offsets == first.pdf_page_offsets


def test_run_marker_cache_key_is_content_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    pdf_a = _write_fake_pdf(tmp_path / "a.pdf", b"%PDF-A-CONTENT\n")
    pdf_b = _write_fake_pdf(tmp_path / "b.pdf", b"%PDF-B-DIFFERENT-CONTENT\n")

    def fake_impl(pdf_path: Path, *, use_llm: bool, extract_images: bool) -> MarkerResult:
        return MarkerResult(markdown=f"# {pdf_path.name}\n", pdf_page_count=1, pdf_page_offsets=[0])

    monkeypatch.setattr(marker_runner, "_run_marker_impl", fake_impl)

    run_marker_cached(pdf_a, cache_dir=cache)
    run_marker_cached(pdf_b, cache_dir=cache)

    md_files = sorted(cache.glob("*.md"))
    assert len(md_files) == 2, f"expected 2 cache files; got {md_files}"
    # Filenames are content hashes — they should differ.
    assert md_files[0].stem != md_files[1].stem
