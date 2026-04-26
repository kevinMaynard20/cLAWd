"""Handlebars-flavored template rendering (spec §4.3 prompt body).

Backed by :mod:`pybars` (the pybars3 package), which implements the Handlebars
template language in pure Python. We use the subset actually needed by the
prompt catalog:

- ``{{var}}`` / ``{{nested.path}}`` — escaped variable interpolation.
- ``{{{var}}}`` — unescaped interpolation (rarely needed; pybars supports it).
- ``{{#each list}} ... {{/each}}`` with ``{{this}}`` / ``{{this.field}}`` inside.
- ``{{#if flag}} ... {{/if}}`` truthiness gating.

pybars3 is tolerant to a fault: unclosed block helpers *compile* silently and
produce empty output at render time. To catch obvious template bugs during
development we do a cheap block-balance check here before handing the body to
pybars; genuine pybars syntax errors (mismatched closers, malformed tags)
bubble up through `TemplateRenderError`.
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import TYPE_CHECKING, Any

import pybars

if TYPE_CHECKING:
    from primitives.prompt_loader import PromptTemplate


class TemplateRenderError(RuntimeError):
    """Raised when a prompt template fails to compile or render.

    Wraps the underlying `pybars.PybarsError` (or balance-check failure) with a
    message that's useful in user-facing error toasts and CostEvent feature
    labels.
    """


# ---------------------------------------------------------------------------
# Block-balance pre-check
# ---------------------------------------------------------------------------

# pybars3 silently swallows an unclosed `{{#each}}` / `{{#if}}`. We catch those
# cases with a lightweight syntactic check that counts block opens and closes
# by helper name. The regex is intentionally loose (it matches built-in block
# helpers `each`, `if`, `unless`, `with`, and any custom block) so the check
# is useful even for unfamiliar templates.

_BLOCK_OPEN_RE = re.compile(r"\{\{#(?P<name>[A-Za-z_][A-Za-z0-9_-]*)")
_BLOCK_CLOSE_RE = re.compile(r"\{\{/(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


def _assert_blocks_balance(body: str) -> None:
    opens = _BLOCK_OPEN_RE.findall(body)
    closes = _BLOCK_CLOSE_RE.findall(body)

    # Pairwise tally: each open must be matched by a close of the same name.
    # Order-sensitive would require a full parser; balance-by-count catches the
    # common mistake (unclosed `{{#each}}`) without false-flagging the OK case
    # of interleaved block types.
    from collections import Counter

    open_counts = Counter(opens)
    close_counts = Counter(closes)
    missing_closes = {k: open_counts[k] - close_counts[k] for k in open_counts}
    stray_closes = {k: close_counts[k] - open_counts[k] for k in close_counts}

    unbalanced = {k: v for k, v in missing_closes.items() if v > 0}
    orphan = {k: v for k, v in stray_closes.items() if v > 0}
    if unbalanced:
        raise TemplateRenderError(
            f"Malformed template: unclosed block helper(s) {unbalanced!r}. "
            f"Each {{{{#helper}}}} must have a matching {{{{/helper}}}}."
        )
    if orphan:
        raise TemplateRenderError(
            f"Malformed template: stray block-close tag(s) {orphan!r} with no "
            f"matching open."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_compiler = pybars.Compiler()

# pybars3's `Compiler.compile` is NOT thread-safe: it mutates singleton state
# during compilation and concurrent calls clobber each other (see the
# `_result.grow` AttributeError that surfaced when React StrictMode's
# double-mount fired two simultaneous chat-turn requests). Compiled templates
# themselves ARE safe to call from multiple threads — they're pure functions
# of (context). So we cache by body-hash and serialise only the compile step.
_compile_lock = threading.Lock()
_compiled_cache: dict[str, Any] = {}


def _compile_cached(body: str) -> Any:
    key = hashlib.sha256(body.encode("utf-8")).hexdigest()
    cached = _compiled_cache.get(key)
    if cached is not None:
        return cached
    with _compile_lock:
        # Re-check under lock — another thread may have populated the cache
        # while we waited.
        cached = _compiled_cache.get(key)
        if cached is not None:
            return cached
        compiled = _compiler.compile(body)
        _compiled_cache[key] = compiled
        return compiled


def render_template(template: PromptTemplate, context: dict[str, Any]) -> str:
    """Render a :class:`PromptTemplate`'s body against the given context.

    `context` is the variable namespace for the Handlebars template: keys
    accessed as ``{{name}}`` resolve to `context["name"]`, and dotted access
    like ``{{case_opinion_block.id}}`` resolves nested mappings. Anything
    JSON-serializable is fair game; non-mapping objects work as long as their
    attributes are accessible as dict keys (pybars does a dict-style lookup).

    Raises :class:`TemplateRenderError` for any compilation or render failure.
    """
    body = template.body
    _assert_blocks_balance(body)

    try:
        compiled = _compile_cached(body)
    except pybars.PybarsError as exc:
        raise TemplateRenderError(
            f"Failed to compile template {template.identifier!r}: {exc}"
        ) from exc

    try:
        rendered = compiled(context)
    except Exception as exc:  # pybars raises a variety of runtime errors
        raise TemplateRenderError(
            f"Failed to render template {template.identifier!r}: {exc}"
        ) from exc

    return str(rendered)


__all__ = [
    "TemplateRenderError",
    "render_template",
]
