"""Symmetric encryption for secrets we store at rest.

Right now that means Google OAuth tokens. A refresh token is a bearer credential
for the user's entire mailbox and calendar with no expiry of its own — a database
dump that leaks them is materially worse than one that leaks the rest of the
schema, so they do not sit in plaintext columns.

Keys are supplied as a comma-separated list in `GOOGLE_TOKEN_ENCRYPTION_KEYS`.
The **first** key encrypts; every key decrypts. That is what `MultiFernet` gives
us, and it is the whole reason for the list: rotating a key means prepending the
new one and leaving the old one in place, after which any row rewritten in the
normal course of business (every token refresh rewrites its row) silently moves
to the new key. No data migration, no downtime, and no window where half the
rows are unreadable. Drop the old key once you are satisfied nothing still needs
it.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core.config import settings


class EncryptionUnavailable(RuntimeError):
    """No usable key was configured, so nothing can be encrypted or decrypted."""


class DecryptionFailed(RuntimeError):
    """Ciphertext did not decrypt under any configured key.

    Almost always a key that was rotated out too early, or a value written by a
    different deployment. Deliberately distinct from `InvalidToken` so callers
    can tell "this row is unreadable" apart from "this token is expired", which
    are different problems with different fixes.
    """


@lru_cache
def _fernet() -> MultiFernet:
    raw = [key.strip() for key in settings.GOOGLE_TOKEN_ENCRYPTION_KEYS.split(",")]
    keys = [key for key in raw if key]
    if not keys:
        raise EncryptionUnavailable(
            "GOOGLE_TOKEN_ENCRYPTION_KEYS is not set; generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`'
        )
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except (ValueError, TypeError) as exc:
        # A malformed key is a config error, and it must not surface later as a
        # confusing per-row decrypt failure.
        raise EncryptionUnavailable(f"invalid key in GOOGLE_TOKEN_ENCRYPTION_KEYS: {exc}") from exc


def encrypt(value: str) -> str:
    """Encrypt a string under the primary key. Returns URL-safe base64 text."""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypt a string written by `encrypt`, trying every configured key."""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionFailed("ciphertext did not decrypt under any configured key") from exc


def is_configured() -> bool:
    """Whether encryption is usable, without raising.

    For startup checks and health endpoints, which want to report a missing key
    rather than crash on the first user who tries to connect their account.
    """
    try:
        _fernet()
    except EncryptionUnavailable:
        return False
    return True
