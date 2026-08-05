"""S3-compatible implementation of the media-storage boundary.

Covers AWS S3, Cloudflare R2, and MinIO without branching on the vendor: they
speak the same API, and what differs is only the endpoint and how a bucket is
addressed within it. Everything bucket-shaped stops here — botocore's exception
taxonomy, presigning, and the 404-vs-403 distinction on a missing object are all
translated into the neutral types in `base.py`.

Self-hosted endpoints bring two wrinkles AWS does not have, both handled below:
buckets have to be addressed as a path rather than a subdomain, and the address
the browser uses may not be the one this process uses.
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


def _build(endpoint: str | None):
    if not settings.S3_BUCKET:
        raise StorageError("S3_BUCKET is not configured")
    if not (settings.S3_ACCESS_KEY_ID and settings.S3_SECRET_ACCESS_KEY):
        raise StorageError("S3 credentials are not configured")
    # A public endpoint with no internal one is always a half-finished config,
    # never a deployment: the bucket cannot be on AWS for us and somewhere else
    # for the browser. Left alone it fails in the worst possible place —
    # presigning uses the public endpoint and succeeds, so the upload goes
    # through, and only the confirmation afterwards discovers it has been
    # talking to real AWS all along. By then the user has recorded a meeting
    # and lost it. Refusing up front turns that into an obvious message.
    if settings.S3_PUBLIC_ENDPOINT_URL.strip() and not settings.S3_ENDPOINT_URL.strip():
        raise StorageError(
            "S3_PUBLIC_ENDPOINT_URL is set but S3_ENDPOINT_URL is not — set the "
            "endpoint the API itself uses (http://minio:9000 for local MinIO), "
            "or clear both to use AWS"
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        region_name=settings.S3_REGION or None,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        config=Config(
            # SigV4 is what R2 requires and what presigned PUTs need in every
            # region created since 2014. Left to default, botocore may pick
            # SigV2 and the signature is rejected.
            signature_version="s3v4",
            # Virtual-host addressing turns a bucket into a subdomain, which
            # only works when the endpoint is a real domain with wildcard DNS.
            # Against a self-hosted endpoint it produces `bucket.minio:9000`
            # and fails to resolve, so anything with a custom endpoint gets
            # path style. AWS keeps the default.
            s3={"addressing_style": "path"} if endpoint else {},
        ),
    )


@lru_cache(maxsize=1)
def _client():
    """For calls this process makes itself — head, delete."""
    return _build(settings.S3_ENDPOINT_URL)


@lru_cache(maxsize=1)
def _presign_client():
    """For URLs handed to a browser.

    Separate from `_client` only when the bucket answers on a different address
    from outside — MinIO in Docker being the case that needs it. The signature
    covers the host, so signing with the internal endpoint would hand out URLs
    that fail with SignatureDoesNotMatch the moment a browser opens them, and
    signing everything with the public one would break head/delete from inside
    the network. Two clients, each correct for its side.
    """
    public = settings.S3_PUBLIC_ENDPOINT_URL.strip()
    if not public or public == settings.S3_ENDPOINT_URL.strip():
        return _client()
    return _build(public)


class S3Storage:
    def presign_put(
        self,
        key: str,
        *,
        content_type: str,
        exact_bytes: int | None = None,
        ttl_seconds: int | None = None,
    ) -> PresignedUpload:
        ttl = ttl_seconds or settings.MEDIA_URL_TTL_SECONDS
        params: dict = {
            "Bucket": settings.S3_BUCKET,
            "Key": key,
            "ContentType": content_type,
        }
        if exact_bytes is not None:
            # Signed, so the browser must send a matching Content-Length. A
            # client that declares 10 MB and streams 10 GB is refused by the
            # bucket, not discovered by us afterwards from a storage bill.
            # Omitted when the size genuinely isn't known yet: signing a bound
            # the browser cannot meet would reject every legitimate upload,
            # which is a worse failure than checking the size on confirmation.
            params["ContentLength"] = exact_bytes
        try:
            url = _presign_client().generate_presigned_url(
                "put_object",
                Params=params,
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
            url = _presign_client().generate_presigned_url(
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
