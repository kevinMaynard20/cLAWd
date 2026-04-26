"""Credentials API routes — spec §7.7.1, §7.7.3.

The first-run wall (§7.7.1) and Settings → API Key page (§7.7.3) drive these
endpoints. Key rules:

- Accept raw keys in request bodies but NEVER echo them back. Responses carry
  the masked display only (`sk-ant-…XXXX`).
- Validation is a separate action — the POST /credentials/<provider> endpoint
  stores the key; the POST /credentials/test endpoint validates the stored
  key against the provider.
- DELETE is an auth-free local action — the app is single-user and bound to
  127.0.0.1 (§7.6), so "authentication" would be ceremony.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from credentials import keyring_backend
from credentials.validation import (
    ValidationResult,
    validate_anthropic,
    validate_voyage,
)

router = APIRouter(prefix="/credentials", tags=["credentials"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StoreKeyRequest(BaseModel):
    """Paste or upload-file body. The backend trims whitespace before storing."""

    key: str = Field(..., min_length=1, description="Raw API key; masked in logs.")


class StatusResponse(BaseModel):
    anthropic_display: str | None = None
    voyage_display: str | None = None
    last_validated_at: datetime | None = None
    last_validation_ok: bool | None = None
    anthropic_present: bool = False
    voyage_present: bool = False


class StoreKeyResponse(BaseModel):
    """Returned after successfully storing + validating a key."""

    display: str
    validation: ValidationResult


class ValidateRequest(BaseModel):
    provider: Literal["anthropic", "voyage"] = "anthropic"


class ClearResponse(BaseModel):
    cleared: Literal["anthropic", "voyage"]


# ---------------------------------------------------------------------------
# Status / load
# ---------------------------------------------------------------------------


@router.get("/status", response_model=StatusResponse)
def get_status() -> StatusResponse:
    """Show what's stored (masked) without validating. Cheap; used on every
    UI navigation to the settings page."""
    creds = keyring_backend.load_credentials()
    return StatusResponse(
        anthropic_display=creds.anthropic_display(),
        voyage_display=creds.voyage_display(),
        last_validated_at=creds.last_validated_at,
        last_validation_ok=creds.last_validation_ok,
        anthropic_present=creds.anthropic_api_key is not None,
        voyage_present=creds.voyage_api_key is not None,
    )


# ---------------------------------------------------------------------------
# Store (paste/upload)
# ---------------------------------------------------------------------------


@router.post("/anthropic", response_model=StoreKeyResponse)
async def store_anthropic(payload: StoreKeyRequest) -> StoreKeyResponse:
    try:
        keyring_backend.store_anthropic_key(payload.key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Key not stored: {exc}",
        ) from exc
    result = await validate_anthropic(payload.key)
    display = keyring_backend.load_credentials().anthropic_display() or ""
    return StoreKeyResponse(display=display, validation=result)


@router.post("/voyage", response_model=StoreKeyResponse)
async def store_voyage(payload: StoreKeyRequest) -> StoreKeyResponse:
    try:
        keyring_backend.store_voyage_key(payload.key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Key not stored: {exc}",
        ) from exc
    result = await validate_voyage(payload.key)
    display = keyring_backend.load_credentials().voyage_display() or ""
    return StoreKeyResponse(display=display, validation=result)


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


@router.delete("/anthropic", response_model=ClearResponse)
def clear_anthropic() -> ClearResponse:
    keyring_backend.clear_anthropic_key()
    return ClearResponse(cleared="anthropic")


@router.delete("/voyage", response_model=ClearResponse)
def clear_voyage() -> ClearResponse:
    keyring_backend.clear_voyage_key()
    return ClearResponse(cleared="voyage")


# ---------------------------------------------------------------------------
# Test stored key
# ---------------------------------------------------------------------------


@router.post("/test", response_model=ValidationResult)
async def test_key(payload: ValidateRequest) -> ValidationResult:
    """Validate a currently-stored key against its provider (spec §7.7.3:
    "Test the current key")."""
    creds = keyring_backend.load_credentials()
    if payload.provider == "anthropic":
        if creds.anthropic_api_key is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No Anthropic key stored. POST to /credentials/anthropic first.",
            )
        return await validate_anthropic(creds.anthropic_api_key.get_secret_value())

    if payload.provider == "voyage":
        if creds.voyage_api_key is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No Voyage key stored. POST to /credentials/voyage first.",
            )
        return await validate_voyage(creds.voyage_api_key.get_secret_value())

    # pydantic Literal should prevent this, but belt-and-suspenders:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown provider {payload.provider!r}",
    )


# ---------------------------------------------------------------------------
# Introspection (used by the cost badge to decide whether LLM features are usable)
# ---------------------------------------------------------------------------


class LlmGateResponse(BaseModel):
    """Spec §7.7.1: `No feature that requires an LLM call is enabled until a
    valid key is stored.`"""

    llm_enabled: bool
    reason: str


@router.get("/gate", response_model=LlmGateResponse)
def llm_gate() -> LlmGateResponse:
    creds = keyring_backend.load_credentials()
    if creds.anthropic_api_key is None:
        return LlmGateResponse(
            llm_enabled=False,
            reason="No Anthropic API key stored. Go to Settings → API Key.",
        )
    # Voyage is optional; no key => BM25 fallback (spec §7.7.3).
    return LlmGateResponse(
        llm_enabled=True,
        reason=(
            "Anthropic key present; LLM features enabled."
            if creds.voyage_api_key is not None
            else "Anthropic key present; Voyage absent — semantic retrieval uses BM25 fallback."
        ),
    )


# ---------------------------------------------------------------------------
# Unused-import guard: keep `datetime`, `timezone` reachable for type-checkers
# ---------------------------------------------------------------------------

_ = (datetime, timezone)
