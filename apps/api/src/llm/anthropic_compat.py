"""Anthropic SDK compatibility helpers.

The SDK's ``client.messages.create(...)`` is what every feature module calls
directly (and the ``primitives.generate`` primitive too). Some Anthropic
models reject parameters that older models accepted — most notably,
``claude-opus-4-7`` rejects ``temperature`` with a 400 ``invalid_request_error``
("``temperature`` is deprecated for this model").

This module wraps the SDK call once so feature code doesn't have to know
which parameters are valid for which model. Add to the deprecation list as
Anthropic publishes more model-specific behaviour.
"""

from __future__ import annotations

import re
from typing import Any

# Regex over model IDs that DO NOT accept ``temperature``. As of 2026-04 this
# is just Opus 4.7 (the spec's default for most features); the pattern is
# written to also catch future Opus 4.x and 5+ releases without code edits.
# When Anthropic publishes additional deprecations this is the place to update.
MODELS_WITHOUT_TEMPERATURE: re.Pattern[str] = re.compile(
    r"^claude-opus-(?:4-(?:[7-9])|[5-9]-)\b",
)


def model_supports_temperature(model: str) -> bool:
    """Return True if ``temperature`` may be passed to this model."""
    return MODELS_WITHOUT_TEMPERATURE.match(model) is None


# Anthropic SDK refuses non-streaming requests whose projected generation
# time exceeds ~10 minutes. With prompts at the model's full output budget
# (Opus 32K, Sonnet 64K), real generations easily reach that threshold and
# the SDK surfaces a ``ValueError("Streaming is required for operations
# that may take longer than 10 minutes.")``. We route any high-budget call
# through the streaming API and collect the final Message, returning the
# same shape feature code already consumes — callers don't need to change.
_STREAMING_TOKEN_THRESHOLD = 8000


def create_message(client: Any, **kwargs: Any) -> Any:
    """Drop-in replacement for ``client.messages.create(**kwargs)`` that:

    1. Strips ``temperature`` for models that don't accept it (Opus 4.7+).
       The feature's intended sampling behaviour is recorded in the prompt
       template (``model_defaults.temperature``); when the model ignores it
       we degrade silently — the model's own defaults are deterministic
       enough for JSON-shaped outputs.

    2. Switches to the SDK's streaming API for high-budget calls. The
       threshold (``_STREAMING_TOKEN_THRESHOLD``) is conservative — any
       prompt template at its model's max can take >10 min to generate at
       real-world output rates, and the SDK then refuses the non-streaming
       call. ``client.messages.stream(...).get_final_message()`` returns
       the same Message shape as ``create()`` so callers get a uniform
       response.

    Test FakeClients in unit tests only stub ``messages.create()``; we
    detect that with ``hasattr`` and fall through to the synchronous path
    for them, so tests don't have to add streaming-mock plumbing.

    Any kwargs the SDK doesn't recognise pass through unchanged so a future
    Anthropic API addition (caching headers, etc.) doesn't require an edit
    here.
    """
    model = str(kwargs.get("model", ""))
    if "temperature" in kwargs and not model_supports_temperature(model):
        kwargs = {k: v for k, v in kwargs.items() if k != "temperature"}

    max_tokens = int(kwargs.get("max_tokens", 0) or 0)
    if max_tokens > _STREAMING_TOKEN_THRESHOLD and hasattr(
        client.messages, "stream"
    ):
        with client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    return client.messages.create(**kwargs)


__all__ = [
    "MODELS_WITHOUT_TEMPERATURE",
    "create_message",
    "model_supports_temperature",
]
