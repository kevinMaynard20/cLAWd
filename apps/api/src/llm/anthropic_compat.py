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


def create_message(client: Any, **kwargs: Any) -> Any:
    """Drop-in replacement for ``client.messages.create(**kwargs)`` that
    sanitises kwargs against per-model deprecations.

    Strips ``temperature`` when the target model doesn't accept it. The
    feature's intended sampling behaviour is recorded in the prompt template
    (``model_defaults.temperature``); when the model ignores it we degrade
    silently — the model's own defaults are still deterministic enough for
    JSON-shaped outputs.

    Any kwargs the SDK doesn't recognise pass through unchanged so a future
    Anthropic API addition (caching headers, etc.) doesn't require an edit
    here.
    """
    model = str(kwargs.get("model", ""))
    if "temperature" in kwargs and not model_supports_temperature(model):
        kwargs = {k: v for k, v in kwargs.items() if k != "temperature"}
    return client.messages.create(**kwargs)


__all__ = [
    "MODELS_WITHOUT_TEMPERATURE",
    "create_message",
    "model_supports_temperature",
]
