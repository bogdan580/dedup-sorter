"""Host/Origin/mutating-request checks (pure, header-dict based)."""
from __future__ import annotations

from tidy.review import security as review_security


# --- security checks (pure, header-dict based) ---------------------------

class TestCheckHost:
    def test_passes_with_127_0_0_1(self):
        assert review_security._check_host({"Host": "127.0.0.1:8123"}, port=8123) is None

    def test_passes_with_localhost_alias(self):
        assert review_security._check_host({"Host": "localhost:8123"}, port=8123) is None

    def test_rejects_missing_host_header(self):
        assert review_security._check_host({}, port=8123) is not None

    def test_rejects_wrong_hostname(self):
        assert review_security._check_host({"Host": "evil.example:8123"}, port=8123) is not None

    def test_rejects_wrong_port(self):
        assert review_security._check_host({"Host": "127.0.0.1:9999"}, port=8123) is not None


class TestCheckOrigin:
    def test_none_when_header_absent(self):
        assert review_security._check_origin({}, port=8123) is None

    def test_passes_with_matching_origin(self):
        assert review_security._check_origin({"Origin": "http://127.0.0.1:8123"}, port=8123) is None

    def test_passes_with_localhost_alias(self):
        assert review_security._check_origin({"Origin": "http://localhost:8123"}, port=8123) is None

    def test_rejects_wrong_origin(self):
        error = review_security._check_origin({"Origin": "http://evil.example:9999"}, port=8123)
        assert error is not None


class TestCheckMutatingRequest:
    def test_passes_with_matching_token_and_host(self):
        error = review_security._check_mutating_request(
            {"X-Dedup-Token": "tok", "Host": "127.0.0.1:8123"},
            port=8123,
            expected_token="tok",
        )
        assert error is None

    def test_passes_with_matching_origin_present(self):
        error = review_security._check_mutating_request(
            {"X-Dedup-Token": "tok", "Host": "127.0.0.1:8123", "Origin": "http://127.0.0.1:8123"},
            port=8123,
            expected_token="tok",
        )
        assert error is None

    def test_rejects_missing_token(self):
        error = review_security._check_mutating_request(
            {"Host": "127.0.0.1:8123"}, port=8123, expected_token="tok",
        )
        assert error is not None

    def test_rejects_wrong_token(self):
        error = review_security._check_mutating_request(
            {"X-Dedup-Token": "wrong", "Host": "127.0.0.1:8123"}, port=8123, expected_token="tok",
        )
        assert error is not None

    def test_rejects_wrong_host(self):
        error = review_security._check_mutating_request(
            {"X-Dedup-Token": "tok", "Host": "evil.example:8123"}, port=8123, expected_token="tok",
        )
        assert error is not None

    def test_rejects_wrong_origin_even_with_correct_host(self):
        # Simulates DNS rebinding: Host can be forced to match by an
        # attacker page whose own hostname resolves to 127.0.0.1, but the
        # browser-set Origin still carries the attacker's real origin.
        error = review_security._check_mutating_request(
            {
                "X-Dedup-Token": "tok",
                "Host": "127.0.0.1:8123",
                "Origin": "http://evil.example:9999",
            },
            port=8123,
            expected_token="tok",
        )
        assert error is not None


class TestHardeningHeaders:
    def test_csp_blocks_framing_and_forms(self):
        csp = review_security._CSP_HEADER
        for directive in ("frame-ancestors 'none'", "base-uri 'none'",
                          "form-action 'none'", "object-src 'none'"):
            assert directive in csp

    def test_missing_token_is_rejected_without_error(self):
        error = review_security._check_mutating_request(
            {"Host": "127.0.0.1:8123"}, port=8123, expected_token="tok",
        )
        assert error is not None

    def test_non_ascii_token_is_rejected_without_error(self):
        error = review_security._check_mutating_request(
            {"X-Dedup-Token": "t\u00f3k", "Host": "127.0.0.1:8123"}, port=8123, expected_token="tok",
        )
        assert error is not None
