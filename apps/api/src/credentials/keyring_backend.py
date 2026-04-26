"""OS keyring storage for API keys, with an encrypted-file fallback.

Spec: §7.7.1, §7.7.2, §3.13.

Primary backend: the `keyring` library, which delegates to:
- macOS Keychain
- Windows Credential Manager
- Linux Secret Service (GNOME Keyring, KWallet)

Service name: ``law-school-study-system``. Entries: ``anthropic-api-key`` and
``voyage-api-key``.

Fallback backend: an encrypted file at
``~/.config/law-school-study-system/credentials.enc``. The symmetric key used
for ``cryptography.fernet.Fernet`` is derived via HKDF-SHA256 from the user's
home directory path, the machine hostname, and a fixed salt.

Security posture (per spec §7.7.2, verbatim): the fallback is
**"enough to prevent casual snooping by other users on a shared machine, not
a high-security solution"**. The salt is hardcoded on purpose — a local
attacker with read access to both this source file and the user's home path
can recover keys. Anyone with real security needs should run a platform with
a functioning keyring backend (macOS/Windows/GNOME).

Env-var overrides (for tests and CI):
- ``LAWSCHOOL_CREDENTIALS_FILE``  — overrides the encrypted-file path.
- ``LAWSCHOOL_FORCE_FILE_BACKEND=1`` — skip the keyring entirely; always use
  the file backend. Useful in CI and for deterministic fallback testing.

Public API (see spec §7.7.1, §7.7.2):
    store_anthropic_key, get_anthropic_key, clear_anthropic_key
    store_voyage_key,    get_voyage_key,    clear_voyage_key
    load_credentials

Design notes (not specified):
- Raw keys are *never* logged, never put into exception messages, never
  stringified outside ``SecretStr``. Whitespace is trimmed at store time so
  "upload a file containing the key" works regardless of trailing newlines.
- Keys shorter than 1 char after trimming are rejected with ``ValueError``
  (empty-string "keys" would otherwise silently persist).
- ``load_credentials`` leaves ``last_validated_at`` / ``last_validation_ok``
  as None; validation lives in ``credentials.validation``.
"""

import json
import logging
import os
import socket
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from data.models import Credentials

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_NAME = "law-school-study-system"
ANTHROPIC_ENTRY = "anthropic-api-key"
VOYAGE_ENTRY = "voyage-api-key"

# Fixed salt for HKDF. See module docstring for the threat-model caveat.
_FALLBACK_SALT = b"law-school-study-system/credentials.enc/v1"
_FALLBACK_INFO = b"fernet-encryption-key"

_DEFAULT_FALLBACK_DIR = Path.home() / ".config" / "law-school-study-system"
_DEFAULT_FALLBACK_FILE = _DEFAULT_FALLBACK_DIR / "credentials.enc"

# JSON blob keys inside the encrypted file
_ANTHROPIC_KEY = "anthropic"
_VOYAGE_KEY = "voyage"


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def _force_file_backend() -> bool:
    return os.environ.get("LAWSCHOOL_FORCE_FILE_BACKEND", "") == "1"


def _fallback_file_path() -> Path:
    override = os.environ.get("LAWSCHOOL_CREDENTIALS_FILE")
    if override:
        return Path(override)
    return _DEFAULT_FALLBACK_FILE


def _clean_key(key: str) -> str:
    """Trim whitespace/newlines. Reject empty/whitespace-only input."""
    trimmed = key.strip()
    if not trimmed:
        raise ValueError("API key is empty after trimming whitespace")
    return trimmed


# ---------------------------------------------------------------------------
# Keyring backend
# ---------------------------------------------------------------------------


def _keyring_available() -> bool:
    """Return True iff the OS keyring can be used right now.

    Guards against:
    - ``LAWSCHOOL_FORCE_FILE_BACKEND=1`` (test/CI override)
    - ``keyring`` not installed (shouldn't happen; belt-and-suspenders)
    - ``NullKeyring`` being active (no backend found on the system)
    """
    if _force_file_backend():
        return False
    try:
        import keyring
        from keyring.backends.fail import Keyring as NullKeyring
    except ImportError:
        return False
    try:
        active = keyring.get_keyring()
    except Exception:  # defensive; keyring init can raise on exotic systems
        return False
    return not isinstance(active, NullKeyring)


def _keyring_set(entry: str, value: str) -> None:
    import keyring

    keyring.set_password(SERVICE_NAME, entry, value)


def _keyring_get(entry: str) -> str | None:
    import keyring

    return keyring.get_password(SERVICE_NAME, entry)


def _keyring_delete(entry: str) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE_NAME, entry)
    except PasswordDeleteError:
        # "not present" is not an error for clear_*(); deletion is idempotent.
        pass


# ---------------------------------------------------------------------------
# Encrypted-file fallback backend
# ---------------------------------------------------------------------------


def _derive_fernet_key() -> bytes:
    """HKDF-SHA256 → 32 bytes → urlsafe_b64 → Fernet key.

    Salts with a fixed constant plus (home dir, hostname). Same machine ↔ same
    key. See module docstring for the threat-model note.
    """
    home = str(Path.home()).encode("utf-8")
    host = socket.gethostname().encode("utf-8")
    ikm = home + b"\x00" + host
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_FALLBACK_SALT,
        info=_FALLBACK_INFO,
    )
    derived = hkdf.derive(ikm)
    # Fernet requires urlsafe-base64-encoded 32-byte key.
    import base64

    return base64.urlsafe_b64encode(derived)


def _fernet() -> Fernet:
    return Fernet(_derive_fernet_key())


def _read_file_blob() -> dict[str, str | None]:
    """Read the encrypted file. Returns a dict with both keys set to None if
    the file is missing, empty, or unreadable."""
    path = _fallback_file_path()
    empty: dict[str, str | None] = {_ANTHROPIC_KEY: None, _VOYAGE_KEY: None}
    if not path.exists() or path.stat().st_size == 0:
        return empty
    try:
        ciphertext = path.read_bytes()
        plaintext = _fernet().decrypt(ciphertext)
        data = json.loads(plaintext.decode("utf-8"))
    except (InvalidToken, json.JSONDecodeError, OSError):
        # Corrupt or machine-mismatched file — log without secrets, start fresh.
        logger.warning(
            "Could not decrypt credentials file at %s; starting empty.", path
        )
        return empty
    # Normalize structure: tolerate partial data.
    return {
        _ANTHROPIC_KEY: data.get(_ANTHROPIC_KEY),
        _VOYAGE_KEY: data.get(_VOYAGE_KEY),
    }


def _write_file_blob(blob: dict[str, str | None]) -> None:
    path = _fallback_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {_ANTHROPIC_KEY: blob.get(_ANTHROPIC_KEY), _VOYAGE_KEY: blob.get(_VOYAGE_KEY)},
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = _fernet().encrypt(payload)
    # Write+rename for atomicity (avoid leaving a half-written file if
    # interrupted mid-write).
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(ciphertext)
    # Best-effort owner-only permissions; ignored on platforms without chmod.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _file_set(entry: str, value: str) -> None:
    blob = _read_file_blob()
    blob[_entry_to_blob_key(entry)] = value
    _write_file_blob(blob)


def _file_get(entry: str) -> str | None:
    return _read_file_blob()[_entry_to_blob_key(entry)]


def _file_delete(entry: str) -> None:
    blob = _read_file_blob()
    blob[_entry_to_blob_key(entry)] = None
    # If both are None, collapse to an empty file to reduce surface area.
    if blob[_ANTHROPIC_KEY] is None and blob[_VOYAGE_KEY] is None:
        path = _fallback_file_path()
        if path.exists():
            try:
                path.unlink()
                return
            except OSError:
                pass
    _write_file_blob(blob)


def _entry_to_blob_key(entry: str) -> str:
    if entry == ANTHROPIC_ENTRY:
        return _ANTHROPIC_KEY
    if entry == VOYAGE_ENTRY:
        return _VOYAGE_KEY
    raise ValueError(f"Unknown credential entry: {entry!r}")


# ---------------------------------------------------------------------------
# Dispatch helpers (try keyring first, fall back to file on failure)
# ---------------------------------------------------------------------------


def _set(entry: str, value: str) -> None:
    if _keyring_available():
        try:
            _keyring_set(entry, value)
            return
        except Exception:  # keyring.errors.* subclass tree; RuntimeError; etc.
            # Do not include the key in the log message.
            logger.warning(
                "Keyring write failed for entry %r; falling back to file backend.",
                entry,
            )
    _file_set(entry, value)


def _get(entry: str) -> str | None:
    if _keyring_available():
        try:
            value = _keyring_get(entry)
        except Exception:
            logger.warning(
                "Keyring read failed for entry %r; trying file backend.", entry
            )
            value = None
        if value is not None:
            return value
        # If keyring returned None, fall through to file backend so that keys
        # written during a prior file-backend session are still discoverable.
    return _file_get(entry)


def _delete(entry: str) -> None:
    if _keyring_available():
        try:
            _keyring_delete(entry)
        except Exception:
            logger.warning(
                "Keyring delete failed for entry %r; trying file backend.", entry
            )
    # Also clear the file copy, in case a prior fallback wrote it.
    try:
        _file_delete(entry)
    except Exception:
        logger.warning("File-backend delete failed for entry %r.", entry)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def store_anthropic_key(key: str) -> None:
    """Persist the Anthropic API key. Trims whitespace before storing."""
    cleaned = _clean_key(key)
    _set(ANTHROPIC_ENTRY, cleaned)


def get_anthropic_key() -> SecretStr | None:
    """Return the stored Anthropic key as ``SecretStr``, or None if unset."""
    raw = _get(ANTHROPIC_ENTRY)
    return SecretStr(raw) if raw else None


def clear_anthropic_key() -> None:
    """Remove the stored Anthropic key (idempotent)."""
    _delete(ANTHROPIC_ENTRY)


def store_voyage_key(key: str) -> None:
    """Persist the Voyage AI API key. Trims whitespace before storing."""
    cleaned = _clean_key(key)
    _set(VOYAGE_ENTRY, cleaned)


def get_voyage_key() -> SecretStr | None:
    """Return the stored Voyage key as ``SecretStr``, or None if unset."""
    raw = _get(VOYAGE_ENTRY)
    return SecretStr(raw) if raw else None


def clear_voyage_key() -> None:
    """Remove the stored Voyage key (idempotent)."""
    _delete(VOYAGE_ENTRY)


def load_credentials() -> Credentials:
    """Assemble the in-memory :class:`Credentials` envelope from storage.

    Validation (``last_validated_at`` / ``last_validation_ok``) is handled by
    ``credentials.validation``; this loader leaves both fields as None.
    """
    return Credentials(
        anthropic_api_key=get_anthropic_key(),
        voyage_api_key=get_voyage_key(),
        last_validated_at=None,
        last_validation_ok=None,
    )


__all__ = [
    "ANTHROPIC_ENTRY",
    "SERVICE_NAME",
    "VOYAGE_ENTRY",
    "clear_anthropic_key",
    "clear_voyage_key",
    "get_anthropic_key",
    "get_voyage_key",
    "load_credentials",
    "store_anthropic_key",
    "store_voyage_key",
]
