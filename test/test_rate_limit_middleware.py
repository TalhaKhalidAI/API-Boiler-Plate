# tests/test_rate_limit_middleware.py
"""
Tests for App.middleware.rate_limit_middleware.resolve_client_ip

Run:
    pytest tests/test_rate_limit_middleware.py -v

The tests exercise the trusted-proxy-aware XFF resolution logic.
Each test class covers one scenario we care about.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

# Import under test
from App.middleware.rate_limit_middleware import (
    resolve_client_ip,
    _is_trusted_peer,
    _trusted_proxies,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(
    *,
    client_host: str | None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """
    Build a minimal Starlette-like Request object good enough for
    resolve_client_ip. Only `client.host` and `headers.get` are accessed.
    """
    request = MagicMock()

    if client_host is None:
        request.client = None
    else:
        request.client = MagicMock()
        request.client.host = client_host

    headers = headers or {}
    # Case-insensitive lookup, matching Starlette behavior
    lower = {k.lower(): v for k, v in headers.items()}
    request.headers = MagicMock()
    request.headers.get = lambda name, default=None: lower.get(name.lower(), default)

    return request


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure TRUSTED_PROXIES doesn't leak between tests."""
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    yield


# ===========================================================================
# 1. No proxy — direct client
# ===========================================================================

class TestNoProxy:
    """request.client.host is the client. XFF must be ignored entirely."""

    def test_returns_peer_when_no_xff(self):
        req = _make_request(client_host="203.0.113.42")
        assert resolve_client_ip(req) == "203.0.113.42"

    def test_ignores_client_supplied_xff(self):
        # Client sends a lie; no proxy in front; peer is real client.
        req = _make_request(
            client_host="203.0.113.42",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        assert resolve_client_ip(req) == "203.0.113.42"

    def test_ignores_client_supplied_xff_chain(self):
        # Client sends a whole fake chain.
        req = _make_request(
            client_host="203.0.113.42",
            headers={"X-Forwarded-For": "8.8.8.8, 1.1.1.1, 9.9.9.9"},
        )
        assert resolve_client_ip(req) == "203.0.113.42"

    def test_no_client_object(self):
        # Some proxies / test harnesses produce request.client = None.
        req = _make_request(client_host=None)
        assert resolve_client_ip(req) == "unknown"


# ===========================================================================
# 2. One trusted proxy — peer is trusted
# ===========================================================================

class TestOneProxyTrusted:
    """client -> proxy -> API. Peer is the trusted proxy. Return rightmost
    non-trusted XFF entry = client."""

    @pytest.fixture(autouse=True)
    def set_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "192.168.100.3")

    def test_single_xff_entry(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "192.168.100.4"},
        )
        assert resolve_client_ip(req) == "192.168.100.4"

    def test_ignores_client_spoofed_prefix(self):
        # Client injected 8.8.8.8, proxy appended real client 192.168.100.4.
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "8.8.8.8, 192.168.100.4"},
        )
        assert resolve_client_ip(req) == "192.168.100.4"

    def test_ignores_multi_spoof(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "8.8.8.8, 9.9.9.9, 1.1.1.1, 192.168.100.4"},
        )
        assert resolve_client_ip(req) == "192.168.100.4"

    def test_no_xff_falls_back_to_peer(self):
        # Trusted proxy did not set XFF (unusual but possible).
        req = _make_request(client_host="192.168.100.3")
        assert resolve_client_ip(req) == "192.168.100.3"

    def test_whitespace_in_xff_is_stripped(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "8.8.8.8 ,   192.168.100.4  "},
        )
        assert resolve_client_ip(req) == "192.168.100.4"

    def test_empty_xff_entries_skipped(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "8.8.8.8,,192.168.100.4"},
        )
        assert resolve_client_ip(req) == "192.168.100.4"


# ===========================================================================
# 3. Two trusted proxies — peer and one more hop
# ===========================================================================

class TestTwoProxiesTrusted:
    """client -> proxy1 -> proxy2 -> API. Peer is proxy2.
    XFF = [client, proxy1]. Walk right-to-left, skip trusted, return client."""

    @pytest.fixture(autouse=True)
    def set_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "192.168.100.3,192.168.100.5")

    def test_two_hop_returns_client(self):
        # XFF: client=192.168.100.4, proxy1=192.168.100.3
        # Peer: proxy2=192.168.100.5
        req = _make_request(
            client_host="192.168.100.5",
            headers={"X-Forwarded-For": "192.168.100.4, 192.168.100.3"},
        )
        assert resolve_client_ip(req) == "192.168.100.4"

    def test_two_hop_with_spoof(self):
        req = _make_request(
            client_host="192.168.100.5",
            headers={"X-Forwarded-For": "8.8.8.8, 192.168.100.4, 192.168.100.3"},
        )
        assert resolve_client_ip(req) == "192.168.100.4"


# ===========================================================================
# 4. Untrusted peer — XFF must be ignored
# ===========================================================================

class TestUntrustedPeer:
    """If the peer is not in TRUSTED_PROXIES, use the peer as client."""

    @pytest.fixture(autouse=True)
    def set_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "192.168.100.3")

    def test_untrusted_peer_ignores_xff(self):
        # Peer is 10.0.0.1, not in trusted list.
        req = _make_request(
            client_host="10.0.0.1",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        assert resolve_client_ip(req) == "10.0.0.1"

    def test_untrusted_peer_with_full_spoof_chain(self):
        req = _make_request(
            client_host="10.0.0.1",
            headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8, 9.9.9.9"},
        )
        assert resolve_client_ip(req) == "10.0.0.1"


# ===========================================================================
# 5. CIDR ranges in TRUSTED_PROXIES
# ===========================================================================

class TestCIDRMatch:
    """TRUSTED_PROXIES accepts CIDR notation."""

    @pytest.fixture(autouse=True)
    def set_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8,172.16.0.0/12")

    def test_peer_in_cidr(self):
        req = _make_request(
            client_host="10.5.4.3",
            headers={"X-Forwarded-For": "203.0.113.42"},
        )
        assert resolve_client_ip(req) == "203.0.113.42"

    def test_peer_not_in_cidr(self):
        req = _make_request(
            client_host="192.168.1.1",
            headers={"X-Forwarded-For": "203.0.113.42"},
        )
        # 192.168.1.1 not in 10/8 or 172.16/12 → untrusted
        assert resolve_client_ip(req) == "192.168.1.1"

    def test_xff_entry_in_cidr_skipped(self):
        # Peer is trusted; XFF contains a trusted IP too; skip it, return next.
        req = _make_request(
            client_host="10.0.0.5",
            headers={"X-Forwarded-For": "203.0.113.42, 10.1.1.1"},
        )
        assert resolve_client_ip(req) == "203.0.113.42"


# ===========================================================================
# 6. Multiple values in TRUSTED_PROXIES
# ===========================================================================

class TestMultipleTrustedEntries:
    @pytest.fixture(autouse=True)
    def set_trusted(self, monkeypatch):
        monkeypatch.setenv(
            "TRUSTED_PROXIES",
            "192.168.100.3,192.168.100.5,10.0.0.0/8",
        )

    def test_first_trusted(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "1.2.3.4"},
        )
        assert resolve_client_ip(req) == "1.2.3.4"

    def test_second_trusted(self):
        req = _make_request(
            client_host="192.168.100.5",
            headers={"X-Forwarded-For": "1.2.3.4"},
        )
        assert resolve_client_ip(req) == "1.2.3.4"

    def test_cidr_trusted(self):
        req = _make_request(
            client_host="10.99.0.1",
            headers={"X-Forwarded-For": "1.2.3.4"},
        )
        assert resolve_client_ip(req) == "1.2.3.4"


# ===========================================================================
# 7. Malformed XFF / config
# ===========================================================================

class TestMalformedInput:
    @pytest.fixture(autouse=True)
    def set_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "192.168.100.3")

    def test_xff_with_only_commas(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": ",,,"},
        )
        # No valid hops → falls through to peer or x-real-ip
        assert resolve_client_ip(req) == "192.168.100.3"

    def test_xff_garbage_returns_peer_if_no_valid_hops(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "   "},
        )
        assert resolve_client_ip(req) == "192.168.100.3"

    def test_malformed_cidr_in_trusted_is_ignored(self, monkeypatch):
        monkeypatch.setenv(
            "TRUSTED_PROXIES", "192.168.100.3,not-a-cidr-xyz"
        )
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "1.2.3.4"},
        )
        # Malformed CIDR must not crash; peer still trusted.
        assert resolve_client_ip(req) == "1.2.3.4"


# ===========================================================================
# 8. X-Real-IP fallback
# ===========================================================================

class TestXRealIPFallback:
    @pytest.fixture(autouse=True)
    def set_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "192.168.100.3")

    def test_x_real_ip_used_when_xff_empty(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Real-IP": "203.0.113.42"},
        )
        assert resolve_client_ip(req) == "203.0.113.42"

    def test_xff_takes_precedence_over_x_real_ip(self):
        req = _make_request(
            client_host="192.168.100.3",
            headers={
                "X-Forwarded-For": "203.0.113.42",
                "X-Real-IP": "8.8.8.8",
            },
        )
        assert resolve_client_ip(req) == "203.0.113.42"

    def test_x_real_ip_only_used_when_peer_trusted(self):
        # Peer is untrusted → X-Real-IP is ignored, peer returned.
        req = _make_request(
            client_host="10.0.0.1",
            headers={"X-Real-IP": "8.8.8.8"},
        )
        assert resolve_client_ip(req) == "10.0.0.1"


# ===========================================================================
# 9. Realistic full-chain scenarios
# ===========================================================================

class TestRealisticChains:
    """End-to-end shapes matching real deployments."""

    def test_local_dev_no_proxy(self):
        # No TRUSTED_PROXIES set
        req = _make_request(
            client_host="127.0.0.1",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        assert resolve_client_ip(req) == "127.0.0.1"

    def test_nginx_only(self, monkeypatch):
        # client -> nginx(192.168.100.3) -> API
        monkeypatch.setenv("TRUSTED_PROXIES", "192.168.100.3")
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "203.0.113.42"},
        )
        assert resolve_client_ip(req) == "203.0.113.42"

    def test_cloudflare_then_alb(self, monkeypatch):
        # client -> CF(198.51.100.1) -> ALB(10.0.0.5) -> API(10.0.0.10)
        # Peer = ALB. XFF = [client, CF].
        monkeypatch.setenv(
            "TRUSTED_PROXIES", "10.0.0.0/24,198.51.100.0/24"
        )
        req = _make_request(
            client_host="10.0.0.5",
            headers={"X-Forwarded-For": "203.0.113.42, 198.51.100.1"},
        )
        assert resolve_client_ip(req) == "203.0.113.42"

    def test_cloudflare_then_alb_with_spoof(self, monkeypatch):
        # Attacker prefixes a fake IP. CF appends real client. ALB appends CF.
        monkeypatch.setenv(
            "TRUSTED_PROXIES", "10.0.0.0/24,198.51.100.0/24"
        )
        req = _make_request(
            client_host="10.0.0.5",
            headers={
                "X-Forwarded-For": "8.8.8.8, 203.0.113.42, 198.51.100.1"
            },
        )
        assert resolve_client_ip(req) == "203.0.113.42"


# ===========================================================================
# 10. _is_trusted_peer and _trusted_proxies directly
# ===========================================================================

class TestHelpers:
    def test_empty_trusted(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
        assert _trusted_proxies() == ()
        assert _is_trusted_peer("1.2.3.4") is False

    def test_exact_match(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.1,10.0.0.2")
        assert _is_trusted_peer("10.0.0.1") is True
        assert _is_trusted_peer("10.0.0.3") is False

    def test_cidr_match(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")
        assert _is_trusted_peer("10.255.255.254") is True
        assert _is_trusted_peer("11.0.0.1") is False

    def test_whitespace_tolerance(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "  10.0.0.1 , 10.0.0.2  ")
        assert _is_trusted_peer("10.0.0.1") is True
        assert _is_trusted_peer("10.0.0.2") is True

    def test_malformed_cidr_ignored(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "not-a-cidr,10.0.0.1")
        # The malformed entry must not crash; the valid one works.
        assert _is_trusted_peer("10.0.0.1") is True
        assert _is_trusted_peer("10.0.0.99") is False


# ===========================================================================
# 11. Attack regression tests
# ===========================================================================

class TestAttackRegression:
    """Tests that specifically reproduce the vulnerability that was fixed."""

    @pytest.fixture(autouse=True)
    def set_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "192.168.100.3")

    def test_attacker_cannot_choose_own_bucket(self):
        """
        Two requests from the same attacker with different XFF values
        MUST resolve to the same IP. Otherwise the rate limiter is bypassable.
        """
        attacker_real = "192.168.100.4"
        proxy = "192.168.100.3"

        req1 = _make_request(
            client_host=proxy,
            headers={"X-Forwarded-For": f"8.8.8.8, {attacker_real}"},
        )
        req2 = _make_request(
            client_host=proxy,
            headers={"X-Forwarded-For": f"9.9.9.9, {attacker_real}"},
        )

        assert resolve_client_ip(req1) == attacker_real
        assert resolve_client_ip(req2) == attacker_real
        assert resolve_client_ip(req1) == resolve_client_ip(req2)

    def test_attacker_cannot_claim_arbitrary_ip(self):
        """Attacker sends X-Forwarded-For: 8.8.8.8 hoping to be seen as 8.8.8.8."""
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        # With only one XFF entry and it's the trusted proxy's own view,
        # the code will trust it (proxy wrote it). But if the proxy
        # correctly appends the peer, this becomes "<fake>, <real>".
        # Simulate the realistic case:
        req = _make_request(
            client_host="192.168.100.3",
            headers={"X-Forwarded-For": "8.8.8.8, 192.168.100.4"},
        )
        assert resolve_client_ip(req) == "192.168.100.4"

    def test_attacker_cannot_break_rate_limit_by_rotating_xff(self):
        """
        Attacker rotates XFF prefix on every request. All requests must
        resolve to the same client IP (the attacker's real IP).
        """
        proxy = "192.168.100.3"
        attacker = "192.168.100.4"
        seen_ips = set()
        for i in range(1, 101):
            req = _make_request(
                client_host=proxy,
                headers={"X-Forwarded-For": f"10.{i}.{i}.{i}, {attacker}"},
            )
            seen_ips.add(resolve_client_ip(req))
        assert seen_ips == {attacker}, (
            f"Attacker rotated XFF but got {len(seen_ips)} distinct buckets: "
            f"{seen_ips}"
        )