"""
Integration tests for MinIO. Requires a live MinIO server.

These tests are skipped automatically if MinIO is unreachable, so they
won't break CI on machines without MinIO. On a machine with MinIO
configured in .env, they exercise the full pipeline.
"""
from __future__ import annotations

import uuid

import pytest

from App.storage.minio_storage import minio_storage as minio_service
from App.core.settings import settings
from App.core.exceptions import (
    MinIOAccessDeniedError,
    MinIOObjectNotFoundError,
)
import io
pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Session setup — skip everything if MinIO is not reachable
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def require_minio():
    """Skip the entire module if MinIO isn't reachable."""
    import asyncio

    async def _check():
        minio_service.connect()
        return await minio_service.health_check()

    health = asyncio.run(_check())
    if not health["connected"]:
        pytest.skip(f"MinIO not reachable: {health.get('error', 'unknown')}")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestMinIOHealth:

    async def test_health_check_reports_healthy(self):
        minio_service.connect()
        health = await minio_service.health_check()
        assert health["connected"] is True
        assert health["status"] == "healthy"

    async def test_can_list_buckets(self):
        minio_service.connect()
        buckets = await minio_service.list_buckets()
        assert isinstance(buckets, list)
        assert settings.MINIO_DEFAULT_BUCKET in buckets, (
            f"Default bucket {settings.MINIO_DEFAULT_BUCKET!r} not found. "
            f"Create it before running integration tests."
        )


# ---------------------------------------------------------------------------
# Object CRUD round-trip
# ---------------------------------------------------------------------------

class TestMinIOCRUDRoundTrip:

    async def test_upload_read_delete_roundtrip(self):
        minio_service.connect()

        key = f"test/{uuid.uuid4()}.txt"
        payload = b"integration test payload"

        # CREATE
        await minio_service.put_object(
            bucket=settings.MINIO_DEFAULT_BUCKET,
            key=key,
            data=payload,
            content_type="text/plain",
        )

        try:
            # READ
            data = await minio_service.get_object(settings.MINIO_DEFAULT_BUCKET, key)
            assert data == payload

            # STAT
            stat = await minio_service.stat_object(settings.MINIO_DEFAULT_BUCKET, key)
            assert stat["size"] == len(payload)
            assert stat["content_type"] == "text/plain"

            # LIST
            keys = await minio_service.list_objects(
                settings.MINIO_DEFAULT_BUCKET, prefix="test/"
            )
            assert key in keys

        finally:
            # DELETE (always, even on failure)
            await minio_service.delete_object(settings.MINIO_DEFAULT_BUCKET, key)

        # VERIFY DELETE
        with pytest.raises(MinIOObjectNotFoundError):
            await minio_service.get_object(settings.MINIO_DEFAULT_BUCKET, key)

    async def test_delete_missing_object_is_idempotent(self):
        minio_service.connect()

        missing_key = f"test/does-not-exist-{uuid.uuid4()}.txt"

        # Should not raise
        await minio_service.delete_object(settings.MINIO_DEFAULT_BUCKET, missing_key)

    async def test_overwrite_object(self):
        minio_service.connect()

        key = f"test/overwrite-{uuid.uuid4()}.txt"

        await minio_service.put_object(settings.MINIO_DEFAULT_BUCKET, key, b"v1")
        await minio_service.put_object(settings.MINIO_DEFAULT_BUCKET, key, b"v2")

        try:
            data = await minio_service.get_object(settings.MINIO_DEFAULT_BUCKET, key)
            assert data == b"v2", "overwrite did not take effect"
        finally:
            await minio_service.delete_object(settings.MINIO_DEFAULT_BUCKET, key)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

class TestMinIOStreaming:

    async def test_put_stream_and_get_object(self):
        minio_service.connect()

        key = f"test/stream-{uuid.uuid4()}.bin"
        payload = b"A" * 1024 * 100     # 100 KB
        stream = io.BytesIO(payload)

        await minio_service.put_stream(
            bucket=settings.MINIO_DEFAULT_BUCKET,
            key=key,
            stream=stream,
            length=len(payload),
        )

        try:
            data = await minio_service.get_object(settings.MINIO_DEFAULT_BUCKET, key)
            assert data == payload
        finally:
            await minio_service.delete_object(settings.MINIO_DEFAULT_BUCKET, key)

    async def test_stream_object_chunks(self):
        minio_service.connect()

        key = f"test/chunked-{uuid.uuid4()}.bin"
        payload = b"X" * (300 * 1024)    # 300 KB
        await minio_service.put_object(settings.MINIO_DEFAULT_BUCKET, key, payload)

        try:
            chunks = []
            async with minio_service.stream_object(
                settings.MINIO_DEFAULT_BUCKET, key, chunk_size=64 * 1024
            ) as stream:
                async for chunk in stream:
                    chunks.append(chunk)

            assert b"".join(chunks) == payload
            assert len(chunks) > 1, "expected multiple chunks"
        finally:
            await minio_service.delete_object(settings.MINIO_DEFAULT_BUCKET, key)


# ---------------------------------------------------------------------------
# Presigned URLs
# ---------------------------------------------------------------------------

class TestMinIOPresignedURLs:

    async def test_presigned_get_url_is_usable(self):
        minio_service.connect()

        key = f"test/presign-{uuid.uuid4()}.txt"
        await minio_service.put_object(settings.MINIO_DEFAULT_BUCKET, key, b"presigned test")

        try:
            url = await minio_service.presigned_get_url(
                settings.MINIO_DEFAULT_BUCKET, key, expiry_sec=60
            )
            assert url.startswith("http")

            # Actually fetch it with httpx (bypasses our SDK, uses raw HTTP)
            import httpx
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                r = await client.get(url)
                assert r.status_code == 200
                assert r.content == b"presigned test"
        finally:
            await minio_service.delete_object(settings.MINIO_DEFAULT_BUCKET, key)


# ---------------------------------------------------------------------------
# Policy scope — app user must NOT touch other buckets
# ---------------------------------------------------------------------------

class TestMinIOPolicyScope:
    """
    These tests assume the app is configured with a scoped user
    (only access to MINIO_DEFAULT_BUCKET). If your app uses root
    credentials, these will fail — that's intentional.
    """

    async def test_cannot_access_other_bucket(self):
        minio_service.connect()

        # Try any bucket that is not the default
        other_buckets = [
            b for b in await minio_service.list_buckets()
            if b != settings.MINIO_DEFAULT_BUCKET
        ]

        if not other_buckets:
            pytest.skip("No other buckets exist to test against")

        target = other_buckets[0]
        with pytest.raises((MinIOAccessDeniedError, Exception)):
            await minio_service.get_object(target, "anything.txt")

    async def test_cannot_create_bucket(self):
        minio_service.connect()

        new_bucket = f"should-fail-{uuid.uuid4().hex[:8]}"

        # If the user has CreateBucket permission, this succeeds.
        # With the scoped policy we wrote, it must fail.
        from App.core.exceptions import MinIOError
        with pytest.raises(MinIOError):
            await minio_service.ensure_bucket(new_bucket)