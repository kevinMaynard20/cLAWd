"""Validation of Anthropic and Voyage API keys against their providers.

Spec: §7.7.1 (first-run validation UX — Valid / Invalid / Unreachable),
      §7.7.3 (Settings → Test current key),
      §7.7.7 (Voyage calls log as CostEvents).

Design notes (not all of these are explicit in the spec):

- **Anthropic**: ``GET /v1/models`` is the spec-endorsed cheap validation path
  (§7.7.1). We send ``x-api-key`` and the stable ``anthropic-version:
  2023-06-01`` header.
- **Voyage**: there is no dedicated "whoami" endpoint. The simplest auth
  probe is a tiny embedding request against ``/v1/embeddings`` with
  ``model=voyage-3`` and ``input=["ping"]``. This burns a trivial number of
  tokens (spec §7.7.7 says Voyage cost is negligible). We log a WARN-level
  note so the caller can decide whether to persist a ``CostEvent``. This
  module does not persist CostEvents itself — that is another module's job.
- **Mapping from HTTP outcome to :class:`ValidationState`**:
    - 200 → VALID.
    - 401 / 403 → INVALID.
    - Connection error, timeout, DNS failure → UNREACHABLE.
    - Any other status → INVALID with the status code and a terse message.
  The caller UI (§7.7.1) handles retry; this module does not retry.
- **Key safety**: the raw key is never placed in ``ValidationResult.message``
  or in any exception text, even on UNREACHABLE. The ``test_validation_result
  _never_leaks_key`` test enforces this.
"""

import asyncio
import logging
from datetime import UTC, datetime
from enum import Enum

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints and constants
# ---------------------------------------------------------------------------

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"

VOYAGE_EMBEDDINGS_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_VALIDATION_MODEL = "voyage-3"
VOYAGE_VALIDATION_INPUT = ["ping"]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ValidationState(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNREACHABLE = "unreachable"


class ValidationResult(BaseModel):
    """Outcome of a provider validation probe. Safe to log and serialize —
    never contains the raw API key."""

    model_config = ConfigDict(frozen=True)

    state: ValidationState
    message: str
    status_code: int | None = None
    validated_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _result(
    state: ValidationState, message: str, status_code: int | None = None
) -> ValidationResult:
    # Defensive: in case a caller ever threads a key into a message by accident
    # during refactoring, we ensure the object's message field is exactly the
    # literal we pass — no key-substitution happens here. Unit tests pin this.
    return ValidationResult(
        state=state,
        message=message,
        status_code=status_code,
        validated_at=_now(),
    )


def _classify_http(status_code: int, provider: str) -> ValidationResult:
    if status_code == 200:
        return _result(ValidationState.VALID, f"{provider} key accepted.", 200)
    if status_code in (401, 403):
        return _result(
            ValidationState.INVALID,
            f"{provider} rejected the key (HTTP {status_code}).",
            status_code,
        )
    return _result(
        ValidationState.INVALID,
        f"Unexpected {provider} response (HTTP {status_code}).",
        status_code,
    )


def _unreachable(provider: str, exc: Exception) -> ValidationResult:
    # Do NOT include repr(exc) — httpx exceptions never carry the key, but we
    # prefer a belt-and-suspenders rule: keep the message provider-scoped.
    kind = type(exc).__name__
    return _result(
        ValidationState.UNREACHABLE,
        f"Could not reach {provider} ({kind}).",
    )


# ---------------------------------------------------------------------------
# Anthropic validation
# ---------------------------------------------------------------------------


def _anthropic_headers(key: str) -> dict[str, str]:
    return {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "accept": "application/json",
    }


async def validate_anthropic(
    key: str, *, timeout_s: float = 10.0
) -> ValidationResult:
    """Validate an Anthropic API key via ``GET /v1/models``.

    Maps outcomes per the module-level docstring. Never retries.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(
                ANTHROPIC_MODELS_URL, headers=_anthropic_headers(key)
            )
    except httpx.TimeoutException as exc:
        return _unreachable("Anthropic", exc)
    except httpx.TransportError as exc:  # parent of ConnectError, etc.
        return _unreachable("Anthropic", exc)
    except httpx.HTTPError as exc:
        return _unreachable("Anthropic", exc)
    return _classify_http(resp.status_code, "Anthropic")


def validate_anthropic_sync(
    key: str, *, timeout_s: float = 10.0
) -> ValidationResult:
    """Synchronous twin of :func:`validate_anthropic` for CLI / fixture use.

    Uses ``httpx.Client``. If called from inside a running event loop, raises
    ``RuntimeError`` — call the async variant instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no running loop — safe to proceed
    else:
        raise RuntimeError(
            "validate_anthropic_sync() called from inside an event loop; "
            "use validate_anthropic() instead."
        )
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.get(ANTHROPIC_MODELS_URL, headers=_anthropic_headers(key))
    except httpx.TimeoutException as exc:
        return _unreachable("Anthropic", exc)
    except httpx.TransportError as exc:
        return _unreachable("Anthropic", exc)
    except httpx.HTTPError as exc:
        return _unreachable("Anthropic", exc)
    return _classify_http(resp.status_code, "Anthropic")


# ---------------------------------------------------------------------------
# Voyage validation
# ---------------------------------------------------------------------------


def _voyage_headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "accept": "application/json",
    }


_VOYAGE_PAYLOAD = {
    "input": VOYAGE_VALIDATION_INPUT,
    "model": VOYAGE_VALIDATION_MODEL,
}


async def validate_voyage(
    key: str, *, timeout_s: float = 10.0
) -> ValidationResult:
    """Validate a Voyage API key via a tiny ``POST /v1/embeddings`` probe.

    Spec §7.7.7: Voyage embedding cost is negligible, so burning a few tokens
    per validation is acceptable. We emit a WARN log so a caller that tracks
    CostEvents can observe it; we do not persist anything here.
    """
    logger.warning(
        "Voyage validation consumes a small number of tokens (~1 embedding). "
        "Caller should persist a CostEvent (provider=voyage, feature=key_validation)."
    )
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                VOYAGE_EMBEDDINGS_URL,
                headers=_voyage_headers(key),
                json=_VOYAGE_PAYLOAD,
            )
    except httpx.TimeoutException as exc:
        return _unreachable("Voyage", exc)
    except httpx.TransportError as exc:
        return _unreachable("Voyage", exc)
    except httpx.HTTPError as exc:
        return _unreachable("Voyage", exc)
    return _classify_http(resp.status_code, "Voyage")


__all__ = [
    "ANTHROPIC_MODELS_URL",
    "ANTHROPIC_VERSION",
    "VOYAGE_EMBEDDINGS_URL",
    "ValidationResult",
    "ValidationState",
    "validate_anthropic",
    "validate_anthropic_sync",
    "validate_voyage",
]
