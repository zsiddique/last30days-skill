"""Tests for scripts/lib/web_searxng.py — self-hosted SearXNG web search backend."""

from unittest import mock

from lib import web_searxng

_CONFIG = {"LAST30DAYS_SEARXNG_URL": "http://searx.invalid:8080"}


class TestSearxngSearch:
    def test_unconfigured_returns_not_configured_reason(self):
        items, artifact = web_searxng.searxng_search("topic", ("2026-07-13", "2026-08-12"), {})
        assert items == []
        assert artifact["reason"] == "searxng-not-configured"
        assert artifact["web_backend"] == "searxng"

    def test_happy_path(self):
        payload = {"results": [
            {
                "url": "https://example.com/post",
                "title": "First & Best",
                "content": "A snippet about the topic.",
                "publishedDate": "2026-08-01T10:00:00",
            },
        ]}
        with mock.patch.object(web_searxng.http, "get", return_value=payload):
            items, artifact = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG)
        assert len(items) == 1
        item = items[0]
        assert item["id"] == "WX1"
        assert item["url"] == "https://example.com/post"
        assert item["title"] == "First & Best"
        assert item["snippet"] == "A snippet about the topic."
        assert item["source_domain"] == "example.com"
        assert item["date"] == "2026-08-01"
        assert item["relevance"] == web_searxng._RELEVANCE
        assert artifact["web_backend"] == "searxng"
        assert artifact["resultCount"] == 1
        assert "reason" not in artifact

    def test_dedupes_repeated_urls(self):
        payload = {"results": [
            {"url": "https://example.com/post", "title": "A"},
            {"url": "https://example.com/post", "title": "A dup"},
        ]}
        with mock.patch.object(web_searxng.http, "get", return_value=payload):
            items, _ = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG)
        assert len(items) == 1

    def test_count_cap(self):
        payload = {"results": [
            {"url": f"https://example.com/{i}", "title": str(i)} for i in range(5)
        ]}
        with mock.patch.object(web_searxng.http, "get", return_value=payload):
            items, _ = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG, count=2)
        assert len(items) == 2

    # -- HIGH-1 malformed-response regressions: never raises -----------------

    def test_null_results_key_does_not_raise(self):
        with mock.patch.object(web_searxng.http, "get", return_value={"results": None}):
            items, artifact = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG)
        assert items == []
        assert artifact["reason"] == "searxng-search-unavailable"

    def test_non_dict_result_entries_are_skipped(self):
        payload = {"results": [None, "not-a-dict", 42, ["nested"]]}
        with mock.patch.object(web_searxng.http, "get", return_value=payload):
            items, _ = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG)
        assert items == []

    def test_null_or_non_string_url_is_skipped(self):
        payload = {"results": [
            {"url": None, "title": "no url"},
            {"url": 12345, "title": "int url"},
            {"title": "missing url key"},
            {"url": "https://example.com/ok", "title": "ok"},
        ]}
        with mock.patch.object(web_searxng.http, "get", return_value=payload):
            items, _ = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG)
        assert len(items) == 1
        assert items[0]["url"] == "https://example.com/ok"

    def test_non_string_content_field_does_not_raise(self):
        payload = {"results": [
            {"url": "https://example.com/post", "title": "A", "content": 12345},
        ]}
        with mock.patch.object(web_searxng.http, "get", return_value=payload):
            items, _ = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG)
        assert len(items) == 1
        assert items[0]["snippet"] == "12345"

    def test_overflow_published_date_does_not_raise(self):
        payload = {"results": [
            {"url": "https://example.com/a", "title": "A", "publishedDate": "1e400"},
            {"url": "https://example.com/b", "title": "B", "publishedDate": "inf"},
        ]}
        with mock.patch.object(web_searxng.http, "get", return_value=payload):
            items, _ = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG)
        assert len(items) == 2
        assert items[0]["date"] is None
        assert items[1]["date"] is None

    def test_http_error_returns_unavailable(self):
        with mock.patch.object(web_searxng.http, "get", side_effect=Exception("boom")):
            items, artifact = web_searxng.searxng_search(
                "topic", ("2026-07-13", "2026-08-12"), _CONFIG)
        assert items == []
        assert artifact["reason"] == "searxng-search-unavailable"


class TestNormalizeDate:
    def test_bare_year_is_not_treated_as_unix_timestamp(self):
        # "2019" naively parses as a Unix timestamp -> 1970-01-01, which is
        # wrong; the guard should refuse to parse it rather than mis-date it.
        assert web_searxng._normalize_date("2019") is None

    def test_uses_published_local_calendar_date_not_utc(self):
        # An offset well past UTC midnight must not shift onto the next UTC
        # day; the calendar date the publisher used should be preserved.
        assert web_searxng._normalize_date("2026-08-11T23:30:00-08:00") == "2026-08-11"

    def test_plain_iso_date(self):
        assert web_searxng._normalize_date("2026-08-01") == "2026-08-01"

    def test_none_and_empty_return_none(self):
        assert web_searxng._normalize_date(None) is None
        assert web_searxng._normalize_date("") is None


class TestTimeRange:
    def test_same_day_span(self):
        assert web_searxng._time_range(("2026-08-12", "2026-08-12")) == "day"

    def test_one_day_span_is_day(self):
        assert web_searxng._time_range(("2026-08-11", "2026-08-12")) == "day"

    def test_two_day_span_is_week(self):
        assert web_searxng._time_range(("2026-08-10", "2026-08-12")) == "week"

    def test_seven_day_span_is_week(self):
        assert web_searxng._time_range(("2026-08-05", "2026-08-12")) == "week"

    def test_eight_day_span_is_month(self):
        assert web_searxng._time_range(("2026-08-04", "2026-08-12")) == "month"

    def test_thirty_one_day_span_is_month(self):
        assert web_searxng._time_range(("2026-07-12", "2026-08-12")) == "month"

    def test_thirty_two_day_span_is_year(self):
        assert web_searxng._time_range(("2026-07-11", "2026-08-12")) == "year"

    def test_invalid_dates_return_none(self):
        assert web_searxng._time_range(("not-a-date", "2026-08-12")) is None


class TestAutoPrecedence:
    def test_crawl4ai_wins_when_searxng_not_configured(self):
        """grounding's auto branch must still prefer crawl4ai over the
        keyless floor when LAST30DAYS_SEARXNG_URL is unset, even with
        CRAWL4AI_URL configured."""
        from lib import grounding

        with mock.patch.object(
            grounding.web_crawl4ai, "crawl4ai_search",
            return_value=([], {"label": "crawl4ai"}),
        ) as crawl4ai_mock, mock.patch.object(
            grounding.web_searxng, "searxng_search",
        ) as searxng_mock:
            grounding.web_search(
                "topic", ("2026-07-13", "2026-08-12"),
                {"CRAWL4AI_URL": "http://192.168.1.13:11235"},
                backend="auto",
            )
        crawl4ai_mock.assert_called_once()
        searxng_mock.assert_not_called()

    def test_searxng_wins_over_crawl4ai_when_configured(self):
        from lib import grounding

        with mock.patch.object(
            grounding.web_searxng, "searxng_search",
            return_value=([], {"label": "searxng"}),
        ) as searxng_mock, mock.patch.object(
            grounding.web_crawl4ai, "crawl4ai_search",
        ) as crawl4ai_mock:
            grounding.web_search(
                "topic", ("2026-07-13", "2026-08-12"),
                {
                    "LAST30DAYS_SEARXNG_URL": "http://192.168.1.12:8888",
                    "CRAWL4AI_URL": "http://192.168.1.13:11235",
                },
                backend="auto",
            )
        searxng_mock.assert_called_once()
        crawl4ai_mock.assert_not_called()
