"""
Diagnostic tests for the 7 bugs identified in code review.

These tests are DIAGNOSTIC. Some assert current behavior, some assert
desired behavior. Read the docstrings to know what each test tells you.

Run with:
    uv run python -m pytest test/test_bugfixes_verification.py -v
"""

import inspect
import ssl

import pytest

from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Bug 1 — Pool params ignore settings
# ---------------------------------------------------------------------------

class TestBug1PoolParams:
    """Verifies Connector.py respects settings.get_connection_pool_params()."""

    async def test_pool_size_matches_settings(self):
        """If this fails, pool params are hardcoded. Bug 1 NOT fixed."""
        from App.core.Connector import database
        from App.core.settings import settings

        await database.connect()
        try:
            actual = database._engine.pool.size()
            expected = settings.DATABASE_POOL_SIZE
            assert actual == expected, (
                f"Pool size mismatch: expected {expected} from settings, "
                f"got {actual}. Connector.py is hardcoding pool params."
            )
        finally:
            await database.disconnect()

    async def test_max_overflow_matches_settings(self):
        """If this fails, max_overflow is hardcoded. Bug 1 NOT fixed."""
        from App.core.Connector import database
        from App.core.settings import settings

        await database.connect()
        try:
            actual = database._engine.pool._max_overflow
            expected = settings.DATABASE_MAX_OVERFLOW
            assert actual == expected, (
                f"Max overflow mismatch: expected {expected}, got {actual}"
            )
        finally:
            await database.disconnect()

    async def test_pool_recycle_matches_settings(self):
        """If this fails, pool_recycle is hardcoded. Bug 1 NOT fixed."""
        from App.core.Connector import database
        from App.core.settings import settings

        await database.connect()
        try:
            actual = database._engine.pool._recycle
            expected = settings.DATABASE_POOL_RECYCLE
            assert actual == expected, (
                f"Pool recycle mismatch: expected {expected}, got {actual}"
            )
        finally:
            await database.disconnect()


# ---------------------------------------------------------------------------
# Bug 2 — Body size limit bypassable via chunked encoding
# ---------------------------------------------------------------------------

class TestBug2ChunkedBodyBypass:
    """Verifies BodySizeLimitMiddleware rejects chunked encoding."""

    def test_middleware_mentions_chunked(self):
        """
        If this fails, the middleware doesn't check Transfer-Encoding.
        Bug 2 NOT fixed. A client can bypass MAX_BODY_SIZE with chunked.
        """
        from App.middleware import body_size_middleware

        source = inspect.getsource(body_size_middleware)
        assert "transfer-encoding" in source.lower(), (
            "BodySizeLimitMiddleware does not check Transfer-Encoding header. "
            "Chunked requests bypass MAX_BODY_SIZE. Bug 2 NOT fixed."
        )

    async def test_chunked_request_rejected(self):
        """
        Sends a chunked POST and expects 413. If this fails, bug 2 NOT fixed.
        """
        from main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/app/v1/auth/basic_auth/login",
                headers={"Transfer-Encoding": "chunked"},
                content=b'{"username":"x","password":"y"}',
            )
            assert r.status_code == 413, (
                f"Chunked request was not rejected. Got {r.status_code}. "
                "Bug 2 NOT fixed."
            )


# ---------------------------------------------------------------------------
# Bug 3 — MinIO TLS verification disabled unconditionally
# ---------------------------------------------------------------------------

class TestBug3MinioTLS:
    """Verifies MinIO client gates TLS verification on MINIO_SECURE."""

    def test_cert_reqs_is_conditional(self):
        """
        If this fails, cert_reqs=ssl.CERT_NONE is unconditional.
        Bug 3 NOT fixed — TLS verification is disabled even in HTTPS mode.
        """
        from App.storage import minio_storage

        source = inspect.getsource(minio_storage)
        assert "MINIO_SECURE" in source, (
            "minio_service.py does not reference MINIO_SECURE. "
            "cert_reqs is likely hardcoded to CERT_NONE. Bug 3 NOT fixed."
        )

    def test_cert_required_branch_exists(self):
        """Confirms the CERT_REQUIRED branch exists in _build_client."""
        from App.storage import minio_storage

        source = inspect.getsource(minio_storage)
        assert "CERT_REQUIRED" in source, (
            "minio_service.py does not use ssl.CERT_REQUIRED anywhere. "
            "TLS verification is never enabled. Bug 3 NOT fixed."
        )

    def test_https_client_verifies_certs(self, monkeypatch):
        """
        With MINIO_SECURE=True, urllib3 PoolManager's connection_pool_kw
        should hold cert_reqs == CERT_REQUIRED.

        urllib3 v2 stores cert_reqs in connection_pool_kw (a dict) on
        the PoolManager, not as a direct attribute. We inspect that dict.
        """
        from App.core import settings as settings_module
        from App.storage import minio_storage as mod

        monkeypatch.setattr(settings_module.settings, "MINIO_SECURE", True)

        client = mod.AsyncMinIOClient()
        minio_sdk = client._build_client()

        pool_manager = minio_sdk._http

        # Path 1: urllib3 v2 — connection_pool_kw dict
        cpk = getattr(pool_manager, "connection_pool_kw", None)
        if isinstance(cpk, dict):
            actual = cpk.get("cert_reqs")
            assert actual == ssl.CERT_REQUIRED, (
                f"connection_pool_kw['cert_reqs'] should be CERT_REQUIRED, "
                f"got {actual!r}. Full connection_pool_kw keys: "
                f"{list(cpk.keys())}"
            )
            return

        # Path 2: urllib3 v1 — direct attribute
        actual = getattr(pool_manager, "cert_reqs", None)
        assert actual == ssl.CERT_REQUIRED, (
            f"PoolManager.cert_reqs should be CERT_REQUIRED, got {actual!r}. "
            f"PoolManager type: {type(pool_manager)}. "
            f"Inspect attributes with dir() to find the real location."
        )

    def test_http_client_does_not_verify_certs(self, monkeypatch):
        """
        With MINIO_SECURE=False, cert_reqs should be CERT_NONE.
        """
        from App.core import settings as settings_module
        from App.storage import minio_storage as mod

        monkeypatch.setattr(settings_module.settings, "MINIO_SECURE", False)

        client = mod.AsyncMinIOClient()
        minio_sdk = client._build_client()

        pool_manager = minio_sdk._http

        cpk = getattr(pool_manager, "connection_pool_kw", None)
        if isinstance(cpk, dict):
            actual = cpk.get("cert_reqs")
        else:
            actual = getattr(pool_manager, "cert_reqs", None)

        assert actual == ssl.CERT_NONE, (
            f"With MINIO_SECURE=False, cert_reqs should be CERT_NONE, "
            f"got {actual!r}"
        )


# ---------------------------------------------------------------------------
# Bug 4 — revoke_family TTL coupling
# ---------------------------------------------------------------------------

class TestBug4RevokeFamilyTTL:
    """Verifies revoke_family calls pass explicit TTL from settings."""

    def test_revoke_session_passes_ttl(self):
        """
        If this fails, AuthService.revoke_session calls revoke_family
        without a TTL. Latent bug 4 NOT fixed.
        """
        from App.services import auth_service

        source = inspect.getsource(auth_service.AuthService.revoke_session)
        assert "revoke_ttl=settings.REFRESH_TOKEN_TTL_SECONDS" in source, (
            "revoke_session does not pass revoke_ttl explicitly. "
            "Bug 4 NOT fixed. If REFRESH_TOKEN_TTL_SECONDS is ever "
            "bumped beyond 7 days, reuse detection silently breaks."
        )

    def test_revoke_all_sessions_passes_ttl(self):
        """Same check for revoke_all_sessions_for_user."""
        from App.services import auth_service

        source = inspect.getsource(
            auth_service.AuthService.revoke_all_sessions_for_user
        )
        assert "revoke_ttl=settings.REFRESH_TOKEN_TTL_SECONDS" in source, (
            "revoke_all_sessions_for_user does not pass revoke_ttl. Bug 4 NOT fixed."
        )


# ---------------------------------------------------------------------------
# Bug 5 — _is_exempt redundant code
# ---------------------------------------------------------------------------

class TestBug5IsExempt:
    """Verifies _is_exempt has no dead code."""

    def test_no_duplicate_path_check(self):
        """
        If this fails, _is_exempt has `path == p` twice. Cosmetic bug 5.
        """
        from App.middleware import rate_limit_middleware

        source = inspect.getsource(
            rate_limit_middleware.GlobalRateLimitMiddleware._is_exempt
        )
        occurrences = source.count("path == p")
        assert occurrences == 1, (
            f"'path == p' appears {occurrences} times in _is_exempt. "
            f"Expected 1. Bug 5 NOT fixed."
        )

    def test_no_redundant_trailing_check(self):
        """Confirms the trailing `or path in self.EXEMPT_PATHS` is gone."""
        from App.middleware import rate_limit_middleware

        source = inspect.getsource(
            rate_limit_middleware.GlobalRateLimitMiddleware._is_exempt
        )
        assert "or path in self.EXEMPT_PATHS" not in source, (
            "Redundant `or path in self.EXEMPT_PATHS` still present. Bug 5 NOT fixed."
        )


# ---------------------------------------------------------------------------
# Bug 6 — get_db() auto-commit (SKIP: retracted)
# ---------------------------------------------------------------------------

class TestBug6AutoCommit:
    """Documents that Bug 6 was retracted. No assertion."""

    def test_skip(self):
        """
        Bug 6 was retracted. get_db() auto-committing read-only
        requests is not a real problem.
        """
        pytest.skip("Bug 6 was retracted in review. No fix needed.")


# ---------------------------------------------------------------------------
# Bug 7 — Routes unauthenticated by default
# ---------------------------------------------------------------------------

class TestBug7DefaultAuth:
    """
    Verifies router-level default auth. Architectural — optional for solo devs.

    If you deliberately chose NOT to apply the fix (solo dev, disciplined),
    this test will fail. Mark it skipped rather than deleting it, so the
    decision is documented.
    """

    # Uncomment the next line if you deliberately chose to skip Bug 7:
    # @pytest.mark.skip(reason="Bug 7 deliberately not applied (solo dev)")

    def test_authenticated_router_exists(self):
        """
        If this fails, there's no router with a default auth dependency.
        Bug 7 NOT fixed. New routes are unauthenticated by default.
        """
        import App.api.v1 as v1

        source = inspect.getsource(v1)
        assert "dependencies=[Depends(get_current_user)]" in source, (
            "No router-level default auth dependency found in "
            "App/api/v1/__init__.py. Bug 7 NOT fixed. "
            "New routes are public by default. Either apply the fix or "
            "mark this test skipped."
        )