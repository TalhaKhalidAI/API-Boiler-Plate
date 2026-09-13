"""
Unit tests for AsyncMinIOClient.

These tests mock the underlying synchronous MinIO SDK so they run without
a live MinIO server. They verify:
  - error translation (S3Error → typed InfrastructureError subclasses)
  - network failure handling
  - asyncio.to_thread usage (event loop never blocked)
  - idempotent behavior (ensure_bucket, delete_object)
"""
from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from minio.error import S3Error

from App.storage.minio_storage import AsyncMinIOClient
from App.core.exceptions import (
    MinIOAccessDeniedError,
    MinIOBucketNotFoundError,
    MinIOConnectionError,
    MinIOError,
    MinIOObjectNotFoundError,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _s3_error(code: str, message: str = "test") -> S3Error:
    """Build an S3Error with the given code for testing."""
    return S3Error(
        code=code,
        message=message,
        resource="/bucket/key",
        request_id="req-test",
        host_id="host-test",
        response=None,
    )


@pytest.fixture
def client() -> AsyncMinIOClient:
    """A connected client with a mocked underlying SDK."""
    c = AsyncMinIOClient()
    c.connect()
    # Replace the sync SDK client with a MagicMock so we can control returns
    c._client = MagicMock()
    return c


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

class TestErrorTranslation:
    """Each S3Error code maps to the right exception subclass."""

    async def test_no_such_bucket_maps_correctly(self, client):
        client._client.bucket_exists.side_effect = _s3_error("NoSuchBucket")

        with pytest.raises(MinIOBucketNotFoundError):
            await client.bucket_exists("missing")

    async def test_no_such_key_maps_correctly(self, client):
        client._client.get_object.side_effect = _s3_error("NoSuchKey")

        with pytest.raises(MinIOObjectNotFoundError):
            await client.get_object("bucket", "missing.txt")

    async def test_access_denied_maps_correctly(self, client):
        client._client.put_object.side_effect = _s3_error("AccessDenied")

        with pytest.raises(MinIOAccessDeniedError):
            await client.put_object("bucket", "k", b"data")

    async def test_signature_mismatch_maps_to_access_denied(self, client):
        client._client.put_object.side_effect = _s3_error("SignatureDoesNotMatch")

        with pytest.raises(MinIOAccessDeniedError):
            await client.put_object("bucket", "k", b"data")

    async def test_unknown_s3_error_maps_to_base_minio_error(self, client):
        client._client.put_object.side_effect = _s3_error("SomeNewErrorCode")

        with pytest.raises(MinIOError):
            await client.put_object("bucket", "k", b"data")


# ---------------------------------------------------------------------------
# Network failure translation
# ---------------------------------------------------------------------------

class TestNetworkFailures:
    """Connection errors map to MinIOConnectionError."""

    async def test_connection_refused_maps_to_connection_error(self, client):
        client._client.list_buckets.side_effect = ConnectionError("refused")

        with pytest.raises(MinIOConnectionError):
            await client.list_buckets()

    async def test_timeout_maps_to_connection_error(self, client):
        client._client.get_object.side_effect = TimeoutError("timed out")

        with pytest.raises(MinIOConnectionError):
            await client.get_object("bucket", "key")

    async def test_health_check_never_raises(self, client):
        """health_check returns a dict, never raises, even on failure."""
        client._client.list_buckets.side_effect = ConnectionError("down")

        result = await client.health_check()

        assert result["connected"] is False
        assert result["status"] == "error"
        assert "error" in result


# ---------------------------------------------------------------------------
# Bucket operations
# ---------------------------------------------------------------------------

class TestBucketOperations:

    async def test_ensure_bucket_creates_when_missing(self, client):
        client._client.bucket_exists.return_value = False

        await client.ensure_bucket("newbucket")

        client._client.make_bucket.assert_called_once_with("newbucket")

    async def test_ensure_bucket_skips_when_exists(self, client):
        client._client.bucket_exists.return_value = True

        await client.ensure_bucket("existing")

        client._client.make_bucket.assert_not_called()

    async def test_list_buckets_returns_names(self, client):
        b1, b2 = MagicMock(), MagicMock()
        b1.name, b2.name = "python", "uploads"
        client._client.list_buckets.return_value = [b1, b2]

        result = await client.list_buckets()

        assert result == ["python", "uploads"]

    async def test_bucket_exists_returns_bool(self, client):
        client._client.bucket_exists.return_value = True
        assert await client.bucket_exists("python") is True

        client._client.bucket_exists.return_value = False
        assert await client.bucket_exists("python") is False


# ---------------------------------------------------------------------------
# Object operations
# ---------------------------------------------------------------------------

class TestObjectOperations:

    async def test_put_object_returns_key(self, client):
        key = await client.put_object("python", "a.txt", b"hello")
        assert key == "a.txt"
        client._client.put_object.assert_called_once()

    async def test_get_object_returns_bytes(self, client):
        mock_response = MagicMock()
        mock_response.read.return_value = b"file contents"
        client._client.get_object.return_value = mock_response

        data = await client.get_object("python", "a.txt")

        assert data == b"file contents"
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()

    async def test_get_object_closes_response_on_error(self, client):
        mock_response = MagicMock()
        mock_response.read.side_effect = RuntimeError("read failed")
        client._client.get_object.return_value = mock_response

        with pytest.raises(RuntimeError):
            await client.get_object("python", "a.txt")

        # The connection must be released even when reading fails
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()

    async def test_delete_object_is_idempotent(self, client):
        """Deleting a missing object should not raise."""
        client._client.remove_object.side_effect = _s3_error("NoSuchKey")

        # Should complete without raising
        await client.delete_object("python", "missing.txt")

    async def test_delete_object_propagates_access_denied(self, client):
        client._client.remove_object.side_effect = _s3_error("AccessDenied")

        with pytest.raises(MinIOAccessDeniedError):
            await client.delete_object("python", "a.txt")

    async def test_list_objects_returns_keys(self, client):
        obj1, obj2 = MagicMock(), MagicMock()
        obj1.object_name, obj2.object_name = "a.txt", "b.txt"
        client._client.list_objects.return_value = [obj1, obj2]

        keys = await client.list_objects("python")

        assert keys == ["a.txt", "b.txt"]

    async def test_stat_object_returns_dict(self, client):
        mock_stat = MagicMock()
        mock_stat.object_name = "a.txt"
        mock_stat.size = 42
        mock_stat.etag = "abc123"
        mock_stat.content_type = "text/plain"
        mock_stat.last_modified = "2026-09-13T10:00:00Z"
        mock_stat.metadata = {"x-custom": "value"}
        client._client.stat_object.return_value = mock_stat

        result = await client.stat_object("python", "a.txt")

        assert result["key"] == "a.txt"
        assert result["size"] == 42
        assert result["content_type"] == "text/plain"
        assert result["metadata"] == {"x-custom": "value"}


# ---------------------------------------------------------------------------
# Presigned URLs
# ---------------------------------------------------------------------------

class TestPresignedURLs:

    async def test_presigned_get_url_returned(self, client):
        client._client.presigned_get_object.return_value = "https://minio.lab.mli/python/a.txt?sig=xyz"

        url = await client.presigned_get_url("python", "a.txt", expiry_sec=600)

        assert url.startswith("https://")
        assert "a.txt" in url

    async def test_presigned_put_url_returned(self, client):
        client._client.presigned_put_object.return_value = "https://minio.lab.mli/python/a.txt?sig=xyz"

        url = await client.presigned_put_url("python", "a.txt")

        assert url.startswith("https://")

    async def test_presigned_get_translates_access_denied(self, client):
        client._client.presigned_get_object.side_effect = _s3_error("AccessDenied")

        with pytest.raises(MinIOAccessDeniedError):
            await client.presigned_get_url("python", "a.txt")


# ---------------------------------------------------------------------------
# Async behavior — verify blocking calls are offloaded
# ---------------------------------------------------------------------------

class TestAsyncBehavior:

    async def test_put_object_does_not_block_event_loop(self, client):
        """Slow SDK call must not block other coroutines."""
        import time

        def slow_put(*args, **kwargs):
            time.sleep(0.2)   # 200 ms of blocking

        client._client.put_object.side_effect = slow_put

        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.02)
                ticks += 1

        # Run put and ticker concurrently
        await asyncio.gather(
            client.put_object("python", "a.txt", b"data"),
            ticker(),
        )

        # If put blocked the loop, ticker would have run 0 or 1 times
        assert ticks >= 5, "put_object appears to block the event loop"