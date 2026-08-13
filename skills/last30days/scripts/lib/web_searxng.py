"""Keyless web search via a self-hosted SearXNG instance — JSON API, on-infra.

Mirrors ``web_crawl4ai`` but queries a SearXNG metasearch instance
(``LAST30DAYS_SEARXNG_URL``) directly through its JSON API instead of scraping
a DuckDuckGo HTML SERP. SearXNG aggregates results across multiple real search
engines (including keyed ones like Brave when the instance is configured with
them), so it outranks the crawl4ai/DDG-HTML rung while still running on your
own infrastructure. Selectable with ``--web-backend=searxng`` or auto-chosen
when ``LAST30DAYS_SEARXNG_URL`` is set and there is no paid key / native host
search. Returns ``(items, artifact)`` in the same shape as grounding's paid
backends so results flow through normalize/score/dedupe unchanged. Never
raises.
"""

import datetime
import os
import urllib.parse

from . import dates, http
from . import web_search_keyless as wk

# Slightly above the keyless floor (0.6): SearXNG aggregates multiple real
# engines — including a keyed Brave engine when the instance has one
# configured — so its results outrank scraping DuckDuckGo's HTML SERP through
# crawl4ai.
_RELEVANCE = 0.7


def _base_url(config: dict) -> str | None:
    if config and config.get("LAST30DAYS_SEARXNG_URL"):
        return str(config["LAST30DAYS_SEARXNG_URL"]).rstrip("/")
    env_url = os.environ.get("LAST30DAYS_SEARXNG_URL")
    return env_url.rstrip("/") if env_url else None


def _time_range(date_range: tuple[str, str]) -> str | None:
    """Map an ISO (from, to) date range to SearXNG's ``time_range`` param."""
    try:
        start = datetime.date.fromisoformat(date_range[0])
        end = datetime.date.fromisoformat(date_range[1])
    except (TypeError, ValueError, IndexError):
        return None
    span = (end - start).days
    if span <= 1:
        return "day"
    if span <= 7:
        return "week"
    if span <= 31:
        return "month"
    return "year"


def _normalize_date(raw: object) -> str | None:
    try:
        if not raw:
            return None
        text = str(raw).strip()
        # Take the published-local calendar date rather than routing the
        # full timestamp through dates.parse_date's tz-aware UTC conversion
        # (matching grounding.py's brave/exa slicing at raw_date[:10] /
        # split("T")[0]) — otherwise an offset publishedDate near midnight
        # gets shifted onto the neighboring UTC day and out of the window.
        date_part = text.split("T")[0][:10]
        # A bare numeric-looking value (e.g. "2019") parses as a Unix
        # timestamp in dates.parse_date and yields 1970-01-01, not the year
        # it actually names. Real epoch seconds are >= 8 digits; anything
        # shorter is a year/id-shaped value, not a timestamp, so skip it
        # before it reaches dates.parse_date.
        digits = date_part.replace(".", "", 1).lstrip("-")
        if digits.isdigit() and len(digits) < 8:
            return None
        parsed = dates.parse_date(date_part)
        return parsed.date().isoformat() if parsed else None
    except Exception:  # noqa: BLE001 - malformed upstream data must not raise
        return None


def searxng_search(query: str, date_range: tuple[str, str], config: dict, count: int = 8):
    """Run web search through a SearXNG instance; returns (items, artifact). Never raises."""
    base = _base_url(config)
    if not base:
        return [], {
            "label": "searxng",
            "webSearchQueries": [query],
            "resultCount": 0,
            "web_backend": "searxng",
            "reason": "searxng-not-configured",
        }

    params = {"q": query, "format": "json"}
    time_range = _time_range(date_range)
    if time_range:
        params["time_range"] = time_range
    url = f"{base}/search?{urllib.parse.urlencode(params)}"

    try:
        data = http.get(url, headers={"Accept": "application/json"}, timeout=15, retries=2)
    except Exception:  # noqa: BLE001 - service/network errors are non-fatal
        data = None

    candidates: list[dict] = []
    seen: set[str] = set()
    results = data.get("results") if isinstance(data, dict) else None
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict):
            continue
        target = r.get("url")
        if not isinstance(target, str) or not target.startswith("http") or target in seen:
            continue
        seen.add(target)
        candidates.append({
            "title": str(r.get("title") or ""),
            "url": target,
            "source_domain": wk._domain(target),
            "snippet": str(r.get("content") or "")[:500],
            "date": _normalize_date(r.get("publishedDate")),
            "relevance": _RELEVANCE,
            "why_relevant": "searxng web search",
        })

    # Stable-partition dated results ahead of undated ones before truncating.
    # A SearXNG response blends engines, and only some (e.g. braveapi) supply
    # publishedDate. Grounding items are normalized with require_date=True, so
    # an undated item is discarded downstream -- truncating in raw engine order
    # can spend the whole budget on results that cannot survive normalization.
    ordered = [c for c in candidates if c["date"]] + [c for c in candidates if not c["date"]]
    items = [{**c, "id": f"WX{n + 1}"} for n, c in enumerate(ordered[:count])]

    artifact = {
        "label": "searxng",
        "webSearchQueries": [query],
        "resultCount": len(items),
        "web_backend": "searxng",
    }
    if not items:
        artifact["reason"] = "searxng-search-unavailable"
    return items, artifact
