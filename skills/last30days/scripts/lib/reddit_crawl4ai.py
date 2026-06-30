"""Reddit backend via crawl4ai — the datacenter / headless-fleet path.

reddit.com's public ``.json`` endpoints and official API return HTTP 403 from
most datacenter / cloud / VPS IPs. This backend routes the SAME
``old.reddit.com`` ``.json`` endpoints through a crawl4ai service (a real
browser that renders JS + handles anti-bot), so the free Reddit path works at
non-residential egress where ``reddit_public`` would 403.

Selected with ``LAST30DAYS_REDDIT_BACKEND=crawl4ai``. Requires a reachable
crawl4ai service via ``CRAWL4AI_URL`` (default ``http://192.168.1.13:11235``).

Normalization is delegated to ``reddit_public._parse_posts`` so items match the
exact shape the rest of the pipeline consumes; only the transport differs.
Ported from zsiddique/reddit-skill.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import reddit_public
from .reddit import DEPTH_CONFIG, expand_reddit_queries, _extract_core_subject

DEFAULT_CRAWL4AI = "http://192.168.1.13:11235"


def _log(msg: str) -> None:
    sys.stderr.write(f"[RedditCrawl4ai] {msg}\n")
    sys.stderr.flush()


def _base(config=None) -> str:
    """Resolve the crawl4ai base URL: config > env > default."""
    if config and config.get("CRAWL4AI_URL"):
        return str(config["CRAWL4AI_URL"]).rstrip("/")
    return os.environ.get("CRAWL4AI_URL", DEFAULT_CRAWL4AI).rstrip("/")


def _raw(url: str, base: str, timeout: int = 90) -> str:
    """Fetch a URL's raw body through crawl4ai's /md endpoint (f=raw)."""
    body = json.dumps({"url": url, "f": "raw"}).encode()
    req = urllib.request.Request(
        base + "/md", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        return d.get("markdown", "") if isinstance(d, dict) else str(d)
    except Exception as e:  # noqa: BLE001
        _log(f"crawl4ai fetch failed for {url}: {e}")
        return ""


def _fetch_json(url: str, base: str, timeout: int = 90):
    """Fetch a reddit ``.json`` URL through crawl4ai and parse it.

    crawl4ai wraps the body in a markdown code fence; strip it, then parse.
    """
    text = _raw(url, base, timeout)
    if not text:
        return None
    t = re.sub(r"^\s*```[a-zA-Z]*\n", "", text.strip())
    t = re.sub(r"\n```\s*$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
        try:
            return json.loads(m.group(1)) if m else None
        except Exception:
            return None


def _search_once(query, base, depth="default", subreddit=None, timeout=90):
    """One search.json call through crawl4ai, normalized via reddit_public."""
    limit = reddit_public.DEPTH_LIMITS.get(
        depth, reddit_public.DEPTH_LIMITS["default"])
    eq = urllib.parse.quote_plus(query)
    if subreddit:
        sub = subreddit.removeprefix("r/").strip()
        url = (
            f"https://old.reddit.com/r/{sub}/search.json"
            f"?q={eq}&restrict_sr=on&sort=relevance&t=month"
            f"&limit={limit}&raw_json=1"
        )
    else:
        url = (
            f"https://old.reddit.com/search.json"
            f"?q={eq}&sort=relevance&t=month&limit={limit}&raw_json=1"
        )
    return reddit_public._parse_posts(_fetch_json(url, base, timeout))


def _date_str(created_utc):
    if not created_utc:
        return None
    try:
        return datetime.fromtimestamp(
            float(created_utc), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return None


def _enrich(item, base, timeout=90):
    """Attach top_comments + comment_insights via <permalink>.json."""
    url = item.get("url", "")
    if not url:
        return
    j = _fetch_json(url.rstrip("/") + ".json", base, timeout)
    if not isinstance(j, list) or len(j) < 2:
        return
    top, insights = [], []
    for c in j[1].get("data", {}).get("children", [])[:15]:
        cd = c.get("data", {})
        body = cd.get("body", "")
        if not body or body in ("[deleted]", "[removed]"):
            continue
        author = cd.get("author", "[deleted]")
        permalink = cd.get("permalink", "")
        top.append({
            "score": cd.get("ups") or cd.get("score", 0) or 0,
            "date": _date_str(cd.get("created_utc")),
            "author": author,
            "excerpt": body[:400],
            "url": f"https://reddit.com{permalink}" if permalink else "",
        })
        if len(body) >= 30 and author not in (
                "[deleted]", "[removed]", "AutoModerator"):
            insights.append(body[:150])
    top.sort(key=lambda c: c.get("score", 0) or 0, reverse=True)
    item["top_comments"] = top[:10]
    item["comment_insights"] = insights[:10]


def search_reddit_crawl4ai(
    topic, from_date, to_date, depth="default",
    subreddits=None, dedicated_subreddits=None, config=None,
):
    """crawl4ai-routed Reddit search + enrichment.

    Drop-in for ``reddit_public.search_reddit_public``: same args (+ config),
    returns a list of normalized item dicts (empty on total failure so the
    public / ScrapeCreators fallbacks can still engage).
    """
    base = _base(config)
    cfg = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    core = _extract_core_subject(topic) or topic
    queries = expand_reddit_queries(topic, depth)[: cfg["global_searches"] or 1]

    subs = []
    for group in (subreddits or [], dedicated_subreddits or []):
        for s in group:
            if s and s not in subs:
                subs.append(s)

    tasks = [(q, None) for q in queries] + [(core, s) for s in subs]
    posts = []
    if tasks:
        with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as ex:
            futs = {
                ex.submit(_search_once, q, base, depth, sub): (q, sub)
                for (q, sub) in tasks
            }
            for fut in as_completed(futs):
                try:
                    posts.extend(fut.result() or [])
                except Exception as e:  # noqa: BLE001
                    _log(f"search task failed: {e}")

    seen, unique = set(), []
    for p in posts:
        key = p.get("url") or p.get("id")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(p)

    in_range = [
        p for p in unique
        if (p.get("date") is None) or (from_date <= p["date"] <= to_date)
    ]
    items = in_range if in_range else unique

    items.sort(
        key=lambda p: (p.get("engagement", {}) or {}).get("score", 0) or 0,
        reverse=True,
    )
    for i, p in enumerate(items):
        p["id"] = f"R{i + 1}"

    if not items:
        _log("no items (crawl4ai unreachable or empty); caller will fall back")
        return []

    enriched = 0
    enrich_n = cfg.get("comment_enrichments", 5)
    if enrich_n:
        top_items = items[:enrich_n]
        with ThreadPoolExecutor(max_workers=min(4, len(top_items))) as ex:
            list(ex.map(lambda it: _enrich(it, base), top_items))
        enriched = len(top_items)
    _log(f"returned {len(items)} items ({enriched} enriched)")
    return items
