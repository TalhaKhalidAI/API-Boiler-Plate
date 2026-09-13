"""
Failure-mode tests: what happens to the app when MinIO is unreachable.

These mock the MinIO SDK to raise connection errors, then verify the
service raises MinIOConnectionError (which routes translate to 503).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from App.storage.minio_storage import AsyncMinIOClient
from App.core.exceptions import MinIOConnectionError, MinIOError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def dead_client() -> AsyncMinIOClient:
    """A client whose SDK always raises ConnectionError."""
    c = AsyncMinIOClient()
    c.connect()
    c._client = MagicMock()
    c._client.put_object.side_effect = ConnectionError("refused")
    c._client.get_object.side_effect = ConnectionError("refused")
    c._client.list_buckets.side_effect = ConnectionError("refused")
    c._client.bucket_exists.side_effect = ConnectionError("refused")
    return c


class TestFailureModes:

    async def test_put_fails_with_connection_error(self, dead_client):
        with pytest.raises(MinIOConnectionError):
            await dead_client.put_object("python", "k", b"data")

    async def test_get_fails_with_connection_error(self, dead_client):
        with pytest.raises(MinIOConnectionError):
            await dead_client.get_object("python", "k")

    async def test_list_fails_with_connection_error(self, dead_client):
        with pytest.raises(MinIOConnectionError):
            await dead_client.list_buckets()

    async def test_health_check_returns_not_connected(self, dead_client):
        result = await dead_client.health_check()
        assert result["connected"] is False
        assert result["status"] == "error"

    async def test_minio_errors_subclass_infrastructure_error(self):
        """All MinIO errors must be catchable as InfrastructureError."""
        from App.core.exceptions import (
            InfrastructureError,
            MinIOAccessDeniedError,
            MinIOBucketNotFoundError,
            MinIOConnectionError,
            MinIOError,
            MinIOObjectNotFoundError,
        )

        assert issubclass(MinIOError, InfrastructureError)
        assert issubclass(MinIOBucketNotFoundError, InfrastructureError)
        assert issubclass(MinIOObjectNotFoundError, InfrastructureError)
        assert issubclass(MinIOAccessDeniedError, InfrastructureError)
        assert issubclass(MinIOConnectionError, InfrastructureError)