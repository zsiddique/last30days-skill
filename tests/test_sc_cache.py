"""Tests for ScrapeCreators `cache_max_age` support (lib/http.py + lib/env.py).

Covers:
  - cache_max_age injected into params for api.scrapecreators.com requests
  - never injected for other hosts
  - each of the five accepted values passes env-level validation
  - an invalid value is rejected client-side (disabled, no raise)
  - off/none/0/empty disable caching
  - the caller's params dict is never mutated
  - the cache-hit debug line
"""

from unittest.mock import MagicMock, patch

import pytest

from lib import env, http


@pytest.fixture(autouse=True)
def _reset_scrapecreators_cache_setting():
    """`http._scrapecreators_cache_max_age` is process-global (set once at CLI
    startup, see last30days.py) — reset it around every test so one test's
    setting can never leak into another test in this module or the suite."""
    http.set_scrapecreators_cache_max_age(None)
    yield
    http.set_scrapecreators_cache_max_age(None)


def _mock_response(body: str = '{"ok": true}') -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = body.encode("utf-8")
    resp.status = 200
    return resp


class TestParamInjection:
    """`request()` merges cache_max_age into params for ScrapeCreators hosts only."""

    @patch("lib.http.urllib.request.urlopen")
    def test_injected_for_scrapecreators_host(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response()
        http.set_scrapecreators_cache_max_age("7d")
        http.get(
            "https://api.scrapecreators.com/v1/tiktok/profile",
            params={"handle": "x"},
        )
        sent_url = mock_urlopen.call_args[0][0].full_url
        assert "cache_max_age=7d" in sent_url
        assert "handle=x" in sent_url

    @patch("lib.http.urllib.request.urlopen")
    def test_not_injected_for_other_hosts(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response()
        http.set_scrapecreators_cache_max_age("7d")
        http.get("https://api.example.com/search", params={"q": "test"})
        sent_url = mock_urlopen.call_args[0][0].full_url
        assert "cache_max_age" not in sent_url

    @patch("lib.http.urllib.request.urlopen")
    def test_disabled_setting_omits_param_even_for_scrapecreators_host(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response()
        # Setting left at the autouse fixture's default (disabled).
        http.get(
            "https://api.scrapecreators.com/v1/tiktok/profile",
            params={"handle": "x"},
        )
        sent_url = mock_urlopen.call_args[0][0].full_url
        assert "cache_max_age" not in sent_url

    @patch("lib.http.urllib.request.urlopen")
    def test_injected_with_no_other_params(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response()
        http.set_scrapecreators_cache_max_age("1d")
        http.get("https://api.scrapecreators.com/v1/reddit/search")
        sent_url = mock_urlopen.call_args[0][0].full_url
        assert "cache_max_age=1d" in sent_url

    def test_does_not_mutate_callers_params_dict(self):
        http.set_scrapecreators_cache_max_age("30d")
        original = {"query": "topic"}
        merged = http._with_scrapecreators_cache_param(
            "https://api.scrapecreators.com/v1/reddit/search", original
        )
        assert original == {"query": "topic"}
        assert merged == {"query": "topic", "cache_max_age": "30d"}
        assert merged is not original

    def test_none_params_stay_none_when_disabled(self):
        assert (
            http._with_scrapecreators_cache_param(
                "https://api.scrapecreators.com/v1/reddit/search", None
            )
            is None
        )

    def test_params_object_returned_unchanged_for_other_host(self):
        http.set_scrapecreators_cache_max_age("7d")
        original = {"q": "test"}
        result = http._with_scrapecreators_cache_param(
            "https://api.example.com/search", original
        )
        assert result is original


class TestCacheHitLogging:
    """A `"cached": true` response emits one debug-gated `[ScrapeCreators]` line."""

    @patch("lib.http.urllib.request.urlopen")
    def test_logs_cache_hit_when_debug_enabled(self, mock_urlopen, monkeypatch, capsys):
        monkeypatch.setenv("LAST30DAYS_DEBUG", "1")
        mock_urlopen.return_value = _mock_response(
            '{"cached": true, "cached_at": "2026-08-12T00:00:00Z", "credits_charged": 0}'
        )
        http.get("https://api.scrapecreators.com/v1/reddit/search")
        stderr = capsys.readouterr().err
        assert "[ScrapeCreators]" in stderr
        assert "cache hit" in stderr

    @patch("lib.http.urllib.request.urlopen")
    def test_no_log_when_debug_disabled(self, mock_urlopen, monkeypatch, capsys):
        monkeypatch.delenv("LAST30DAYS_DEBUG", raising=False)
        mock_urlopen.return_value = _mock_response('{"cached": true}')
        http.get("https://api.scrapecreators.com/v1/reddit/search")
        stderr = capsys.readouterr().err
        assert "[ScrapeCreators]" not in stderr

    @patch("lib.http.urllib.request.urlopen")
    def test_no_log_for_other_host_even_with_cached_key(self, mock_urlopen, monkeypatch, capsys):
        monkeypatch.setenv("LAST30DAYS_DEBUG", "1")
        mock_urlopen.return_value = _mock_response('{"cached": true}')
        http.get("https://api.example.com/search")
        stderr = capsys.readouterr().err
        assert "[ScrapeCreators]" not in stderr


class TestGetScrapecreatorsCacheMaxAge:
    """Client-side validation in lib/env.py, ahead of the API's own 400."""

    @pytest.mark.parametrize("value", ["1d", "3d", "7d", "14d", "30d"])
    def test_accepts_each_valid_value(self, value):
        assert (
            env.get_scrapecreators_cache_max_age({"LAST30DAYS_SC_CACHE_MAX_AGE": value})
            == value
        )

    def test_case_insensitive_valid_value(self):
        assert (
            env.get_scrapecreators_cache_max_age({"LAST30DAYS_SC_CACHE_MAX_AGE": "7D"})
            == "7d"
        )

    def test_defaults_to_1d_when_key_absent(self):
        assert env.get_scrapecreators_cache_max_age({}) == "1d"

    @pytest.mark.parametrize("value", ["off", "none", "0", "", "OFF", "None"])
    def test_disable_values_return_none(self, value):
        assert (
            env.get_scrapecreators_cache_max_age({"LAST30DAYS_SC_CACHE_MAX_AGE": value})
            is None
        )

    def test_invalid_value_disables_without_raising(self, capsys):
        result = env.get_scrapecreators_cache_max_age({"LAST30DAYS_SC_CACHE_MAX_AGE": "2d"})
        assert result is None
        warning = capsys.readouterr().err
        assert "invalid LAST30DAYS_SC_CACHE_MAX_AGE" in warning
        assert "2d" in warning


class TestEndToEndWiring:
    """env's resolved value flows into http's setter exactly as last30days.py wires it."""

    @patch("lib.http.urllib.request.urlopen")
    def test_valid_config_value_reaches_the_request(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response()
        http.set_scrapecreators_cache_max_age(
            env.get_scrapecreators_cache_max_age({"LAST30DAYS_SC_CACHE_MAX_AGE": "14d"})
        )
        http.get("https://api.scrapecreators.com/v1/instagram/user/reels")
        sent_url = mock_urlopen.call_args[0][0].full_url
        assert "cache_max_age=14d" in sent_url

    @patch("lib.http.urllib.request.urlopen")
    def test_off_reaches_the_request_as_no_param(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response()
        http.set_scrapecreators_cache_max_age(
            env.get_scrapecreators_cache_max_age({"LAST30DAYS_SC_CACHE_MAX_AGE": "off"})
        )
        http.get("https://api.scrapecreators.com/v1/instagram/user/reels")
        sent_url = mock_urlopen.call_args[0][0].full_url
        assert "cache_max_age" not in sent_url
