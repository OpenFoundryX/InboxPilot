"""S3-compatible implementation of the media-storage boundary.

Covers AWS S3 and Cloudflare R2 without branching: they speak the same API, and
the only difference is whether `S3_ENDPOINT_URL` is set. Everything
bucket-shaped stops here — botocore's exception taxonomy, presigning, and the
404-vs-403 distinction on a missing object are all translated into the neutral
types in `base.py`.
"""

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from core.config import settings
from core.logging import get_logger
from integrations.storage.base import PresignedUpload, StoredObject, StorageError

log = get_logger(__name__)

#: Codes a bucket returns for "no such object". S3 answers 404/NoSuchKey when
#: the caller may list the bucket and 403/AccessDenied when it may not — with a
#: key-scoped policy the absent case can arrive as either, so both mean absent.
_MISSING = {"404", "NoSuchKey", "403", "AccessDenied"}


@lru_cache(maxsize=1)
def _client():
    if not settings.S3_BUCKET:
        raise StorageError("S3_BUCKET is not configured")
    if not (settings.S3_ACCESS_KEY_ID and settings.S3_SECRET_ACCESS_KEY):
        raise StorageError("S3 credentials are not configured")
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        region_name=settings.S3_REGION or None,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        # SigV4 is what R2 requires and what presigned PUTs need in every
        # region created since 2014. Left to default, botocore may pick SigV2
        # and the signature is rejected.
        config=Config(signature_version="s3v4"),
    )


class S3Storage:
    def presign_put(
        self, key: str, *, content_type: str, max_bytes: int
    ) -> PresignedUpload:
        ttl = settings.MEDIA_URL_TTL_SECONDS
        try:
            url = _client().generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": settings.S3_BUCKET,
                    "Key": key,
                    "ContentType": content_type,
                    # Signed, so the browser must send a matching
                    # Content-Length. A client that declares 10 MB and streams
                    # 10 GB is refused by the bucket, not discovered by us
                    # afterwards from a storage bill.
                    "ContentLength": max_bytes,
                },
                ExpiresIn=ttl,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Could not presign upload for {key}: {exc}") from exc

        return PresignedUpload(
            url=url,
            key=key,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
            headers={"Content-Type": content_type},
        )

    def presign_get(self, key: str) -> tuple[str, datetime]:
        ttl = settings.MEDIA_URL_TTL_SECONDS
        try:
            url = _client().generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.S3_BUCKET, "Key": key},
                ExpiresIn=ttl,
            )
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Could not presign download for {key}: {exc}") from exc

        return url, datetime.now(timezone.utc) + timedelta(seconds=ttl)

    def head(self, key: str) -> StoredObject | None:
        try:
            data = _client().head_object(Bucket=settings.S3_BUCKET, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _MISSING:
                return None
            raise StorageError(f"Could not stat {key}: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"Could not stat {key}: {exc}") from exc

        return StoredObject(
            key=key,
            size_bytes=int(data.get("ContentLength") or 0),
            content_type=data.get("ContentType"),
        )

    def delete(self, key: str) -> None:
        try:
            _client().delete_object(Bucket=settings.S3_BUCKET, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Could not delete {key}: {exc}") from exc
