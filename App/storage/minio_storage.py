"""
Async wrapper around the synchronous MinIO Python SDK.

The official minio library has no async API. We wrap every blocking call
in asyncio.to_thread() so the event loop is never blocked. The SDK's
underlying urllib3 connection pool is shared across threads, which is
thread-safe and efficient.

All methods raise subclasses of InfrastructureError on failure, so route
handlers already map them to 503 via the existing except clause.
"""
from __future__ import annotations

import asyncio
import io
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator, Optional

from minio import Minio
from minio.error import S3Error
from urllib3.exceptions import MaxRetryError, NewConnectionError

from App.core.settings import settings
from App.core.exceptions import (
    MinIOAccessDeniedError,
    MinIOBucketNotFoundError,
    MinIOConnectionError,
    MinIOError,
    MinIOObjectNotFoundError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------
# The minio SDK raises S3Error for server-side errors. We map its error codes
# to our own domain errors so callers never have to import minio specifics.

_S3_ERROR_MAP = {
    "NoSuchBucket": MinIOBucketNotFoundError,
    "NoSuchKey": MinIOObjectNotFoundError,
    "AccessDenied": MinIOAccessDeniedError,
    "AllAccessDisabled": MinIOAccessDeniedError,
    "InvalidAccessKeyId": MinIOAccessDeniedError,
    "SignatureDoesNotMatch": MinIOAccessDeniedError,
}


def _translate_s3_error(exc: S3Error, operation: str) -> MinIOError:
    """Map a minio S3Error to our InfrastructureError hierarchy."""
    error_cls = _S3_ERROR_MAP.get(exc.code, MinIOError)
    logger.warning(
        f"MinIO {operation} failed: code={exc.code} message={exc.message} "
        f"resource={getattr(exc, 'resource', '?')}"
    )
    return error_cls(f"MinIO {operation} failed: {exc.code}")


def _translate_network_error(exc: Exception, operation: str) -> MinIOError:
    """Map connection-level failures to MinIOConnectionError."""
    logger.warning(f"MinIO {operation} unreachable: {type(exc).__name__}: {exc}")
    return MinIOConnectionError(
        f"Cannot reach object storage during {operation}"
    )


# ---------------------------------------------------------------------------
# Client wrapper
# ---------------------------------------------------------------------------

class AsyncMinIOClient:
    """
    Async facade over the synchronous MinIO SDK.

    All blocking operations run in a thread pool via asyncio.to_thread().
    The underlying Minio client manages an HTTP connection pool that is
    shared across threads safely.
    """

    def __init__(self) -> None:
        self._client: Optional[Minio] = None

    # ----- lifecycle -----

    # def _build_client(self) -> Minio:
    #     """Construct the synchronous MinIO client from settings."""
    #     import ssl
    #     import urllib3
    #     from urllib3 import PoolManager, Retry, Timeout

    #     # DEV ONLY: skip TLS verification for self-signed certs.
    #     # TODO: Replace with MINIO_CA_BUNDLE before production.
    #     urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    #     http_client = PoolManager(
    #         timeout=Timeout(connect=5.0, read=30.0),
    #         retries=Retry(total=3, backoff_factor=0.3),
    #         cert_reqs=ssl.CERT_NONE,   # disables cert verification
    #     )

    #     return Minio(
    #         settings.MINIO_ENDPOINT,
    #         access_key=settings.MINIO_ACCESS_KEY,
    #         secret_key=settings.minio_secret_str,
    #         secure=settings.MINIO_SECURE,
    #         http_client=http_client,
    #     )
    def _build_client(self) -> Minio:
        """Construct the synchronous MinIO client from settings."""
        import ssl
        import urllib3
        from urllib3 import PoolManager, Retry, Timeout

        # TLS policy:
        #   - MINIO_SECURE=True  → verify the certificate (use CA bundle if provided)
        #   - MINIO_SECURE=False → no TLS at all, cert_reqs is irrelevant but set to CERT_NONE
        if settings.MINIO_SECURE:
            cert_reqs = ssl.CERT_REQUIRED
        else:
            cert_reqs = ssl.CERT_NONE
            # Only suppress the warning when we actually skipped verification
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        pool_kwargs = {
            "timeout": Timeout(connect=5.0, read=30.0),
            "retries": Retry(total=3, backoff_factor=0.3),
            "cert_reqs": cert_reqs,
        }

        # If a CA bundle is configured, use it for verification
        if settings.MINIO_SECURE and settings.MINIO_CA_BUNDLE:
            pool_kwargs["ca_certs"] = settings.MINIO_CA_BUNDLE

        http_client = PoolManager(**pool_kwargs)

        return Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.minio_secret_str,
            secure=settings.MINIO_SECURE,
            http_client=http_client,
        )
    
    def connect(self) -> None:
        """Create the client. Lazy, cheap, does not contact the server."""
        if self._client is None:
            self._client = self._build_client()
            logger.info(
                f"MinIO client initialized: endpoint={settings.MINIO_ENDPOINT} "
                f"secure={settings.MINIO_SECURE}"
            )

    def disconnect(self) -> None:
        """Drop the client. Idempotent."""
        self._client = None

    @property
    def client(self) -> Minio:
        if self._client is None:
            self.connect()
        return self._client

    # ----- health -----

    async def health_check(self) -> dict:
        """
        Return {'status': 'healthy'|'error', 'connected': bool}.
        Never raises; intended for readiness probes.
        """
        try:
            await asyncio.to_thread(self.client.list_buckets)
            return {"status": "healthy", "connected": True}
        except Exception as e:
            logger.warning(f"MinIO health check failed: {e}")
            return {"status": "error", "connected": False, "error": str(e)}

    # ----- bucket operations -----

    async def ensure_bucket(self, bucket: str) -> None:
        """Create the bucket if it doesn't exist. Idempotent."""
        def _op() -> None:
            if not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)

        try:
            await asyncio.to_thread(_op)
        except S3Error as e:
            raise _translate_s3_error(e, f"ensure_bucket({bucket})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"ensure_bucket({bucket})") from e

    async def bucket_exists(self, bucket: str) -> bool:
        try:
            return await asyncio.to_thread(self.client.bucket_exists, bucket)
        except S3Error as e:
            raise _translate_s3_error(e, f"bucket_exists({bucket})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"bucket_exists({bucket})") from e

    async def list_buckets(self) -> list[str]:
        try:
            buckets = await asyncio.to_thread(self.client.list_buckets)
            return [b.name for b in buckets]
        except S3Error as e:
            raise _translate_s3_error(e, "list_buckets") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, "list_buckets") from e

    # ----- object operations -----

    async def put_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Upload bytes as an object. Returns the object key."""
        def _op() -> None:
            self.client.put_object(
                bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
                metadata=metadata,
            )

        try:
            await asyncio.to_thread(_op)
            return key
        except S3Error as e:
            raise _translate_s3_error(e, f"put_object({bucket}/{key})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"put_object({bucket}/{key})") from e

    async def put_stream(
        self,
        bucket: str,
        key: str,
        stream: io.IOBase,
        length: int,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """
        Upload from a file-like object with known length.
        Use this for large files instead of put_object(..., data=bytes).
        """
        def _op() -> None:
            self.client.put_object(
                bucket,
                key,
                stream,
                length=length,
                content_type=content_type,
                metadata=metadata,
            )

        try:
            await asyncio.to_thread(_op)
            return key
        except S3Error as e:
            raise _translate_s3_error(e, f"put_stream({bucket}/{key})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"put_stream({bucket}/{key})") from e

    async def get_object(self, bucket: str, key: str) -> bytes:
        """Download an object as bytes. For large files use stream_object()."""
        def _op() -> bytes:
            response = self.client.get_object(bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        try:
            return await asyncio.to_thread(_op)
        except S3Error as e:
            raise _translate_s3_error(e, f"get_object({bucket}/{key})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"get_object({bucket}/{key})") from e

    @asynccontextmanager
    async def stream_object(
        self, bucket: str, key: str, chunk_size: int = 64 * 1024
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """
        Stream an object in chunks without loading it all into memory.

        Usage:
            async with minio.stream_object("uploads", "big.zip") as chunks:
                async for chunk in chunks:
                    await response.write(chunk)
        """
        response = await asyncio.to_thread(self.client.get_object, bucket, key)
        try:
            async def _chunks() -> AsyncIterator[bytes]:
                while True:
                    chunk = await asyncio.to_thread(response.read, chunk_size)
                    if not chunk:
                        break
                    yield chunk

            yield _chunks()
        except S3Error as e:
            raise _translate_s3_error(e, f"stream_object({bucket}/{key})") from e
        finally:
            response.close()
            response.release_conn()

    async def stat_object(self, bucket: str, key: str) -> dict:
        """Return object metadata (size, etag, content_type, last_modified)."""
        def _op() -> dict:
            stat = self.client.stat_object(bucket, key)
            return {
                "key": stat.object_name,
                "size": stat.size,
                "etag": stat.etag,
                "content_type": stat.content_type,
                "last_modified": stat.last_modified,
                "metadata": dict(stat.metadata or {}),
            }

        try:
            return await asyncio.to_thread(_op)
        except S3Error as e:
            raise _translate_s3_error(e, f"stat_object({bucket}/{key})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"stat_object({bucket}/{key})") from e

    async def delete_object(self, bucket: str, key: str) -> None:
        """Delete an object. Idempotent — no error if the object is missing."""
        def _op() -> None:
            self.client.remove_object(bucket, key)

        try:
            await asyncio.to_thread(_op)
        except S3Error as e:
            # remove_object is idempotent for NoSuchKey on most S3 impls,
            # but MinIO may still raise. Treat missing as success.
            if e.code == "NoSuchKey":
                return
            raise _translate_s3_error(e, f"delete_object({bucket}/{key})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"delete_object({bucket}/{key})") from e

    async def list_objects(
        self, bucket: str, prefix: str = "", recursive: bool = True
    ) -> list[str]:
        """List object keys in a bucket under a prefix."""
        def _op() -> list[str]:
            return [
                obj.object_name
                for obj in self.client.list_objects(
                    bucket, prefix=prefix, recursive=recursive
                )
            ]

        try:
            return await asyncio.to_thread(_op)
        except S3Error as e:
            raise _translate_s3_error(e, f"list_objects({bucket})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"list_objects({bucket})") from e

    # ----- presigned URLs -----

    async def presigned_get_url(
        self,
        bucket: str,
        key: str,
        expiry_sec: Optional[int] = None,
    ) -> str:
        """Generate a temporary download URL. Handy for the frontend."""
        expiry = expiry_sec or settings.MINIO_PRESIGN_EXPIRY_SEC

        def _op() -> str:
            return self.client.presigned_get_object(
                bucket, key, expires=timedelta(seconds=expiry)
            )

        try:
            return await asyncio.to_thread(_op)
        except S3Error as e:
            raise _translate_s3_error(e, f"presign_get({bucket}/{key})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"presign_get({bucket}/{key})") from e

    async def presigned_put_url(
        self,
        bucket: str,
        key: str,
        expiry_sec: Optional[int] = None,
    ) -> str:
        """Generate a temporary upload URL. Handy for direct browser uploads."""
        expiry = expiry_sec or settings.MINIO_PRESIGN_EXPIRY_SEC

        def _op() -> str:
            return self.client.presigned_put_object(
                bucket, key, expires=timedelta(seconds=expiry)
            )

        try:
            return await asyncio.to_thread(_op)
        except S3Error as e:
            raise _translate_s3_error(e, f"presign_put({bucket}/{key})") from e
        except (MaxRetryError, NewConnectionError, ConnectionError, TimeoutError) as e:
            raise _translate_network_error(e, f"presign_put({bucket}/{key})") from e


# ---------------------------------------------------------------------------
# Singleton + FastAPI dependency
# ---------------------------------------------------------------------------

minio_storage = AsyncMinIOClient()
