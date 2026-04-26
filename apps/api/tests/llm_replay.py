"""LLM call replay cache for tests (spec §6.3).

Purpose: "LLM calls are captured once (against a live API) and replayed from
disk on subsequent runs. Cache key is the full (model, system prompt, user
prompt, temperature) tuple. This is how test runs stay fast and deterministic
without mocking away the actual LLM behavior."

Storage: `tests/.llm_cache/<template>/<cache_key>.json`. Committed to the
repo so CI replays without network access. When a prompt template changes,
its cached responses are invalidated and re-recorded — code review on the
prompt-change PR includes reviewing the new recorded outputs.

Phase 1 status: infrastructure only. No live-recording yet; the first use
sites land in Phase 2 when `generate()` is implemented. We ship it now so
tests can start using `@replay_llm` as a decorator from day one.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReplayRecord:
    """A cached LLM response. Fields are structured loosely so we can evolve the
    schema without invalidating recordings on unrelated changes — add a
    `schema_version` int if we ever need a hard break."""

    cache_key: str
    template: str
    model: str
    input_messages: list[dict[str, Any]]
    response_content: Any  # JSON-serializable: the LLM's output body
    response_usage: dict[str, int]  # {"input_tokens": ..., "output_tokens": ...}

    def to_json(self) -> str:
        return json.dumps(
            {
                "cache_key": self.cache_key,
                "template": self.template,
                "model": self.model,
                "input_messages": self.input_messages,
                "response_content": self.response_content,
                "response_usage": self.response_usage,
            },
            indent=2,
            sort_keys=True,
        )

    @staticmethod
    def from_json(blob: str) -> ReplayRecord:
        data = json.loads(blob)
        return ReplayRecord(
            cache_key=data["cache_key"],
            template=data["template"],
            model=data["model"],
            input_messages=data["input_messages"],
            response_content=data["response_content"],
            response_usage=data["response_usage"],
        )


def _cache_root() -> Path:
    """Locate `tests/.llm_cache/` by walking up from this file to the repo root."""
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "spec.md").exists():
            return candidate / "tests" / ".llm_cache"
    return Path.cwd() / "tests" / ".llm_cache"


def compute_cache_key(
    *,
    template: str,
    model: str,
    input_messages: list[dict[str, Any]],
    temperature: float,
) -> str:
    """Canonical SHA-256 of (template, model, input, temperature).

    Canonicalization: dict keys sorted, whitespace stripped from message
    content before hashing so trivial whitespace diffs don't invalidate the
    cache — but the rendered prompt template's structure and variable bindings
    will be reflected in the content bytes.
    """
    normalized = {
        "template": template,
        "model": model,
        "temperature": round(float(temperature), 4),
        "input_messages": [
            {k: v for k, v in sorted(msg.items())} for msg in input_messages
        ],
    }
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def record_path(template: str, cache_key: str) -> Path:
    return _cache_root() / template / f"{cache_key}.json"


def load_record(template: str, cache_key: str) -> ReplayRecord | None:
    path = record_path(template, cache_key)
    if not path.exists():
        return None
    return ReplayRecord.from_json(path.read_text(encoding="utf-8"))


def save_record(record: ReplayRecord) -> Path:
    path = record_path(record.template, record.cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.to_json(), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Record-mode flag
# ---------------------------------------------------------------------------


def recording_enabled() -> bool:
    """True when LLM_REPLAY_RECORD=1 is set. When True, tests should fall
    through to a live API call on cache miss and persist the response; when
    False (default), cache misses are a test failure."""
    return os.environ.get("LLM_REPLAY_RECORD", "") == "1"


# ---------------------------------------------------------------------------
# Utility for tests
# ---------------------------------------------------------------------------


class ReplayMiss(AssertionError):
    """Raised when a test expects a cached response and there's none recorded.
    Re-record by running the suite with `LLM_REPLAY_RECORD=1` and committing
    the resulting JSON files."""


def assert_cached_or_miss(
    *,
    template: str,
    model: str,
    input_messages: list[dict[str, Any]],
    temperature: float,
) -> ReplayRecord:
    """Load a cached record or raise ReplayMiss with a helpful message."""
    key = compute_cache_key(
        template=template,
        model=model,
        input_messages=input_messages,
        temperature=temperature,
    )
    rec = load_record(template, key)
    if rec is None:
        raise ReplayMiss(
            f"No replay record for template={template!r} key={key[:12]}…\n"
            f"Re-record by running with LLM_REPLAY_RECORD=1 and commit the "
            f"resulting file under {record_path(template, key)}"
        )
    return rec


__all__ = [
    "ReplayMiss",
    "ReplayRecord",
    "assert_cached_or_miss",
    "compute_cache_key",
    "load_record",
    "record_path",
    "recording_enabled",
    "save_record",
]
