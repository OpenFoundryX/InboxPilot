"""The object-storage vendor boundary.

Holding bytes and handing out expiring links to them is undifferentiated work we
buy, exactly like sending a bot into a call. This module defines the whole
surface of what we buy, so a second provider is a new file next to `s3.py` and a
config change — nothing above `integrations/` knows which vendor is in play.

Only meeting media lives here: browser recordings and uploaded files, the ones
with nowhere else to go. Recall keeps its own bot recordings and signs its own
links; that path does not touch this module.

Provider calls are blocking; call them from Celery workers, or via
`run_in_threadpool` from the API, matching the meeting-bot integration.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


class StorageError(RuntimeError):
    """A storage call failed. Callers decide whether to retry or give up."""


@dataclass(frozen=True)
class PresignedUpload:
    """Permission for a browser to PUT one object, directly.

    The bytes never pass through the API: a 1 GB body would pin a worker for
    minutes and pay egress twice.
    """

    url: str
    key: str
    expires_at: datetime
    # Headers the browser must send for the signature to verify. Content-Type is
    # signed, so a PUT that omits it fails — the client cannot be left to guess.
    headers: dict[str, str]


@dataclass(frozen=True)
class StoredObject:
    """What the bucket knows about an object that exists."""

    key: str
    size_bytes: int
    content_type: str | None = None


@runtime_checkable
class MediaStorage(Protocol):
    """Everything InboxPilot needs from an object store."""

    def presign_put(
        self,
        key: str,
        *,
        content_type: str,
        exact_bytes: int | None = None,
        ttl_seconds: int | None = None,
    ) -> PresignedUpload:
        """A URL the browser can PUT one object to, and nothing else.

        `exact_bytes` pins the size into the signature, so a client that
        declares one size and sends another is refused by the bucket rather
        than trusted by us. It is only available when the size is known in
        advance — an upload of a file that already exists. A recording still
        being made has no size yet, and passing None leaves the length
        unsigned; the size is then checked when the upload is confirmed, which
        catches an oversized object after the fact rather than before.

        `ttl_seconds` overrides the configured lifetime. A live recording needs
        a URL that outlives the meeting, since it is signed when recording
        starts and used when it stops.
        """

    def presign_get(self, key: str) -> tuple[str, datetime]:
        """A playable link and its deadline.

        Returns the pair because a link without its expiry is a link that will
        quietly stop working in a cache — the same reason `RecordingMedia`
        carries `expires_at`.
        """

    def download_to(self, key: str, dest) -> None:
        """Stream an object to a local path, for this process to read.

        Distinct from `presign_get` on purpose. A signed URL is for handing to
        a browser, so it points at wherever the browser reaches the bucket —
        which is not somewhere a worker can necessarily resolve, and is a
        needless signature besides when the caller already holds credentials.
        Anything server-side that needs the bytes uses this.
        """

    def head(self, key: str) -> StoredObject | None:
        """What is actually stored under `key`, or None if nothing is.

        This is how an upload is confirmed. A client's word that it finished is
        not evidence: it would queue a transcription job against an object that
        may never have been written.
        """

    def delete(self, key: str) -> None:
        """Remove an object. Idempotent — deleting what is already gone is fine.

        Retention pruning depends on both halves of that: it must actually free
        the bytes, and it must be safe to run again after a partial failure.
        """
