"""Unit tests for ``credentials.keyring_backend`` and ``credentials.validation``.

Test names map 1:1 to spec §7.7.8 checklist items where applicable.

Uses ``pytest-httpx`` (listed in the ``dev`` optional group) to mock HTTP
traffic. The only test that hits the network is ``test_key_validation_live``,
which is gated by the ``TEST_ANTHROPIC_KEY`` env var and marked
``@pytest.mark.live_api`` so it only runs when explicitly opted in.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from credentials import keyring_backend
from credentials.validation import (
    ANTHROPIC_MODELS_URL,
    VOYAGE_EMBEDDINGS_URL,
    ValidationState,
    validate_anthropic,
    validate_anthropic_sync,
    validate_voyage,
)
from data.models import Credentials

FAKE_ANTHROPIC_KEY = "sk-ant-api03-THIS-IS-A-FAKE-KEY-0000"
FAKE_VOYAGE_KEY = "pa-voyage-FAKE-KEY-0000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def unique_service_name(monkeypatch: pytest.MonkeyPatch) -> str:
    """Override SERVICE_NAME for the duration of a test so real-keyring runs
    don't pollute the user's keychain / credential manager."""
    name = f"law-school-study-system-test-{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(keyring_backend, "SERVICE_NAME", name)
    return name


@pytest.fixture
def force_file_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Route all credential I/O through the encrypted-file backend at
    ``tmp_path/credentials.enc``. Yields the path."""
    creds_path = tmp_path / "credentials.enc"
    monkeypatch.setenv("LAWSCHOOL_FORCE_FILE_BACKEND", "1")
    monkeypatch.setenv("LAWSCHOOL_CREDENTIALS_FILE", str(creds_path))
    return creds_path


def _real_keyring_available() -> bool:
    """True if the OS keyring is a real backend (not NullKeyring)."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as NullKeyring
    except ImportError:
        return False
    try:
        return not isinstance(keyring.get_keyring(), NullKeyring)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Keyring roundtrip (spec §7.7.8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("store_fn", "get_fn", "clear_fn", "key"),
    [
        (
            keyring_backend.store_anthropic_key,
            keyring_backend.get_anthropic_key,
            keyring_backend.clear_anthropic_key,
            FAKE_ANTHROPIC_KEY,
        ),
        (
            keyring_backend.store_voyage_key,
            keyring_backend.get_voyage_key,
            keyring_backend.clear_voyage_key,
            FAKE_VOYAGE_KEY,
        ),
    ],
    ids=["anthropic", "voyage"],
)
def test_keyring_roundtrip(
    unique_service_name: str,
    store_fn,
    get_fn,
    clear_fn,
    key: str,
) -> None:
    """Spec §7.7.8: store a fake key, read it back, clear it."""
    if not _real_keyring_available():
        pytest.skip(
            "No real OS keyring backend available (NullKeyring). "
            "Skipping real-keyring roundtrip; file-backend test covers this path."
        )

    # Make sure we start clean in case a previous run crashed.
    clear_fn()
    assert get_fn() is None

    store_fn(key)
    got = get_fn()
    assert got is not None
    assert isinstance(got, SecretStr)
    assert got.get_secret_value() == key

    clear_fn()
    assert get_fn() is None


# ---------------------------------------------------------------------------
# Encrypted-file fallback
# ---------------------------------------------------------------------------


def test_encrypted_file_fallback(force_file_backend: Path) -> None:
    """Store + read + clear via file backend. Verify bytes on disk are NOT
    plaintext and contain the Fernet version prefix ``gAAAAA``."""
    path = force_file_backend
    keyring_backend.store_anthropic_key(FAKE_ANTHROPIC_KEY)

    assert path.exists(), "File backend should have created the encrypted file."
    data = path.read_bytes()
    # Fernet tokens start with 0x80 0x00 0x00 0x00 0x00 ... which, after
    # urlsafe-base64 encoding, begins with the literal prefix 'gAAAAA'.
    assert data.startswith(b"gAAAAA"), f"File not Fernet-encoded: {data[:16]!r}"
    assert FAKE_ANTHROPIC_KEY.encode() not in data, "Plaintext key found on disk!"

    got = keyring_backend.get_anthropic_key()
    assert got is not None
    assert got.get_secret_value() == FAKE_ANTHROPIC_KEY

    keyring_backend.clear_anthropic_key()
    assert keyring_backend.get_anthropic_key() is None


def test_encrypted_file_stores_both_keys(force_file_backend: Path) -> None:
    """Both keys coexist in the single encrypted blob; each clears independently."""
    keyring_backend.store_anthropic_key(FAKE_ANTHROPIC_KEY)
    keyring_backend.store_voyage_key(FAKE_VOYAGE_KEY)

    a = keyring_backend.get_anthropic_key()
    v = keyring_backend.get_voyage_key()
    assert a is not None and a.get_secret_value() == FAKE_ANTHROPIC_KEY
    assert v is not None and v.get_secret_value() == FAKE_VOYAGE_KEY

    keyring_backend.clear_anthropic_key()
    assert keyring_backend.get_anthropic_key() is None
    # Voyage must remain intact.
    v2 = keyring_backend.get_voyage_key()
    assert v2 is not None and v2.get_secret_value() == FAKE_VOYAGE_KEY

    keyring_backend.clear_voyage_key()
    assert keyring_backend.get_voyage_key() is None


def test_store_key_trims_whitespace(force_file_backend: Path) -> None:
    """§7.7.1: 'upload a file containing the key' — tolerate trailing newlines."""
    keyring_backend.store_anthropic_key(f"  {FAKE_ANTHROPIC_KEY}\n")
    got = keyring_backend.get_anthropic_key()
    assert got is not None
    assert got.get_secret_value() == FAKE_ANTHROPIC_KEY


def test_store_empty_key_rejected(force_file_backend: Path) -> None:
    with pytest.raises(ValueError):
        keyring_backend.store_anthropic_key("   \n  ")


def test_load_credentials_no_keys_stored(force_file_backend: Path) -> None:
    creds = keyring_backend.load_credentials()
    assert isinstance(creds, Credentials)
    assert creds.anthropic_api_key is None
    assert creds.voyage_api_key is None
    assert creds.last_validated_at is None
    assert creds.last_validation_ok is None


def test_load_credentials_with_keys(force_file_backend: Path) -> None:
    keyring_backend.store_anthropic_key(FAKE_ANTHROPIC_KEY)
    keyring_backend.store_voyage_key(FAKE_VOYAGE_KEY)
    creds = keyring_backend.load_credentials()
    assert creds.anthropic_api_key is not None
    assert creds.voyage_api_key is not None
    assert creds.anthropic_api_key.get_secret_value() == FAKE_ANTHROPIC_KEY
    assert creds.voyage_api_key.get_secret_value() == FAKE_VOYAGE_KEY
    # Validation fields are always None here; validation is a separate module.
    assert creds.last_validated_at is None
    assert creds.last_validation_ok is None


# ---------------------------------------------------------------------------
# Validation — mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_validation_mocked_valid(httpx_mock) -> None:
    httpx_mock.add_response(
        url=ANTHROPIC_MODELS_URL,
        method="GET",
        status_code=200,
        json={"data": []},
    )
    result = await validate_anthropic(FAKE_ANTHROPIC_KEY)
    assert result.state is ValidationState.VALID
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_key_validation_mocked_invalid_401(httpx_mock) -> None:
    httpx_mock.add_response(
        url=ANTHROPIC_MODELS_URL,
        method="GET",
        status_code=401,
        json={"error": {"message": "invalid x-api-key"}},
    )
    result = await validate_anthropic(FAKE_ANTHROPIC_KEY)
    assert result.state is ValidationState.INVALID
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_key_validation_mocked_invalid_other_status(httpx_mock) -> None:
    """A 500 (or any non-401/403) must map to INVALID with the status code."""
    httpx_mock.add_response(
        url=ANTHROPIC_MODELS_URL, method="GET", status_code=500
    )
    result = await validate_anthropic(FAKE_ANTHROPIC_KEY)
    assert result.state is ValidationState.INVALID
    assert result.status_code == 500


@pytest.mark.asyncio
async def test_key_validation_mocked_unreachable(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("no route to host"))
    result = await validate_anthropic(FAKE_ANTHROPIC_KEY)
    assert result.state is ValidationState.UNREACHABLE
    assert result.status_code is None


@pytest.mark.asyncio
async def test_key_validation_mocked_timeout(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("slow"))
    result = await validate_anthropic(FAKE_ANTHROPIC_KEY, timeout_s=0.1)
    assert result.state is ValidationState.UNREACHABLE


@pytest.mark.asyncio
async def test_key_validation_voyage_mocked(httpx_mock) -> None:
    """VALID path for Voyage."""
    httpx_mock.add_response(
        url=VOYAGE_EMBEDDINGS_URL,
        method="POST",
        status_code=200,
        json={"data": [{"embedding": [0.0] * 1024}]},
    )
    result = await validate_voyage(FAKE_VOYAGE_KEY)
    assert result.state is ValidationState.VALID
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_key_validation_voyage_mocked_invalid(httpx_mock) -> None:
    """INVALID path for Voyage (401)."""
    httpx_mock.add_response(
        url=VOYAGE_EMBEDDINGS_URL,
        method="POST",
        status_code=401,
        json={"detail": "invalid api key"},
    )
    result = await validate_voyage(FAKE_VOYAGE_KEY)
    assert result.state is ValidationState.INVALID
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_validation_result_never_leaks_key(httpx_mock) -> None:
    """Whatever the outcome, the raw key must not appear in the result message
    or str(result)."""
    # Unreachable branch (no HTTP happens — the raw key has no chance to leak
    # via an echoed response, but the implementation could still stringify it
    # in an exception formatter; we guard against that).
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    result = await validate_anthropic(FAKE_ANTHROPIC_KEY)
    assert FAKE_ANTHROPIC_KEY not in result.message
    assert FAKE_ANTHROPIC_KEY not in str(result)
    assert FAKE_ANTHROPIC_KEY not in repr(result)

    # Invalid branch — test a fresh mock response.
    httpx_mock.add_response(
        url=ANTHROPIC_MODELS_URL, method="GET", status_code=401
    )
    result2 = await validate_anthropic(FAKE_ANTHROPIC_KEY)
    assert FAKE_ANTHROPIC_KEY not in result2.message
    assert FAKE_ANTHROPIC_KEY not in str(result2)


# ---------------------------------------------------------------------------
# Sync wrapper
# ---------------------------------------------------------------------------


def test_validate_anthropic_sync_mocked(httpx_mock) -> None:
    httpx_mock.add_response(
        url=ANTHROPIC_MODELS_URL, method="GET", status_code=200, json={"data": []}
    )
    result = validate_anthropic_sync(FAKE_ANTHROPIC_KEY)
    assert result.state is ValidationState.VALID
    assert result.status_code == 200


# ---------------------------------------------------------------------------
# Live key validation (gated)
# ---------------------------------------------------------------------------


@pytest.mark.live_api
@pytest.mark.asyncio
async def test_key_validation_live() -> None:
    """Spec §7.7.8: runs only when TEST_ANTHROPIC_KEY is set; skipped otherwise."""
    key = os.environ.get("TEST_ANTHROPIC_KEY")
    if not key:
        pytest.skip("TEST_ANTHROPIC_KEY not set; skipping live validation.")
    result = await validate_anthropic(key)
    # We don't assert VALID — the test env key may be revoked — but the state
    # must be a real enum member and not an exception.
    assert result.state in {
        ValidationState.VALID,
        ValidationState.INVALID,
        ValidationState.UNREACHABLE,
    }
