"""Media-storage provider selection.

One place resolves `MEDIA_STORAGE_PROVIDER` to an implementation, so callers
never name a vendor. Adding one means a new module and a line in `_PROVIDERS`.
"""

from functools import lru_cache

from core.config import settings
from integrations.storage.base import (
    MediaStorage,
    PresignedUpload,
    StoredObject,
    StorageError,
)
from integrations.storage.s3 import S3Storage

_PROVIDERS = {"s3": S3Storage}


@lru_cache(maxsize=1)
def get_storage() -> MediaStorage:
    name = (settings.MEDIA_STORAGE_PROVIDER or "").strip().lower()
    factory = _PROVIDERS.get(name)
    if not factory:
        raise StorageError(
            f"Unknown MEDIA_STORAGE_PROVIDER {name!r}; expected one of {sorted(_PROVIDERS)}"
        )
    return factory()


__all__ = [
    "get_storage",
    "MediaStorage",
    "PresignedUpload",
    "StoredObject",
    "StorageError",
]
