"""Smoke tests for the LLM replay cache (spec §6.3, infrastructure-only in Phase 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.llm_replay import (
    ReplayMiss,
    ReplayRecord,
    assert_cached_or_miss,
    compute_cache_key,
    load_record,
    record_path,
    recording_enabled,
    save_record,
)


@pytest.fixture
def redirect_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point `_cache_root()` at a tmp dir by spoofing spec.md presence."""
    # Easier path: monkey-patch the private helper directly.
    import tests.llm_replay as mod

    monkeypatch.setattr(mod, "_cache_root", lambda: tmp_path)
    yield tmp_path


def test_cache_key_is_deterministic() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    k1 = compute_cache_key(
        template="case_brief", model="claude-opus-4-7", input_messages=msgs, temperature=0.2
    )
    k2 = compute_cache_key(
        template="case_brief", model="claude-opus-4-7", input_messages=msgs, temperature=0.2
    )
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex


def test_cache_key_changes_on_template() -> None:
    msgs = [{"role": "user", "content": "hello"}]
    k_brief = compute_cache_key(
        template="case_brief", model="claude-opus-4-7", input_messages=msgs, temperature=0.2
    )
    k_rubric = compute_cache_key(
        template="rubric_from_memo", model="claude-opus-4-7", input_messages=msgs, temperature=0.2
    )
    assert k_brief != k_rubric


def test_cache_key_changes_on_temperature() -> None:
    msgs = [{"role": "user", "content": "x"}]
    k1 = compute_cache_key(template="t", model="m", input_messages=msgs, temperature=0.2)
    k2 = compute_cache_key(template="t", model="m", input_messages=msgs, temperature=0.7)
    assert k1 != k2


def test_save_then_load_roundtrip(redirect_cache: Path) -> None:
    record = ReplayRecord(
        cache_key="abc123",
        template="case_brief",
        model="claude-opus-4-7",
        input_messages=[{"role": "user", "content": "brief Shelley"}],
        response_content={"case_name": "Shelley v. Kraemer"},
        response_usage={"input_tokens": 1200, "output_tokens": 450},
    )
    save_record(record)
    path = record_path("case_brief", "abc123")
    assert path.exists()
    loaded = load_record("case_brief", "abc123")
    assert loaded == record


def test_load_record_missing_returns_none(redirect_cache: Path) -> None:
    assert load_record("case_brief", "missing_key") is None


def test_assert_cached_or_miss_raises_on_miss(redirect_cache: Path) -> None:
    with pytest.raises(ReplayMiss, match="LLM_REPLAY_RECORD=1"):
        assert_cached_or_miss(
            template="case_brief",
            model="claude-opus-4-7",
            input_messages=[{"role": "user", "content": "x"}],
            temperature=0.2,
        )


def test_assert_cached_or_miss_loads_hit(redirect_cache: Path) -> None:
    msgs = [{"role": "user", "content": "hello"}]
    key = compute_cache_key(
        template="case_brief", model="claude-opus-4-7", input_messages=msgs, temperature=0.2
    )
    save_record(
        ReplayRecord(
            cache_key=key,
            template="case_brief",
            model="claude-opus-4-7",
            input_messages=msgs,
            response_content={"holding": "for the plaintiff"},
            response_usage={"input_tokens": 10, "output_tokens": 5},
        )
    )
    rec = assert_cached_or_miss(
        template="case_brief",
        model="claude-opus-4-7",
        input_messages=msgs,
        temperature=0.2,
    )
    assert rec.response_content == {"holding": "for the plaintiff"}


def test_recording_enabled_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_REPLAY_RECORD", raising=False)
    assert recording_enabled() is False
    monkeypatch.setenv("LLM_REPLAY_RECORD", "1")
    assert recording_enabled() is True
    monkeypatch.setenv("LLM_REPLAY_RECORD", "0")
    assert recording_enabled() is False
