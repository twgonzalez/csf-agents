#!/usr/bin/env python3
"""
media_scanner.py — News & Social Media Scanner
California Stewardship Fund

Scans RSS news feeds, NewsAPI, and (stubbed) X API for coverage of California
housing legislation and local control policy. Produces a structured JSON digest
that social_writer.py and newsletter_writer.py can use for news-aware content.

Pipeline position:
    bill_tracker.py → tracked_bills.json → housing_analyzer.py
                                                ↓
                                       media_scanner.py   ← this agent
                                                ↓
                                       social_writer.py
                                       newsletter_writer.py (future)

Output:
    data/media/media_digest.json   ← read by downstream agents

Usage:
    .venv/bin/python agents/media/media_scanner.py            # default (7-day lookback)
    .venv/bin/python agents/media/media_scanner.py --lookback 3
    .venv/bin/python agents/media/media_scanner.py --bills path/to/tracked_bills.json
    .venv/bin/python agents/media/media_scanner.py --dry-run  # scan but don't write

Data sources:
    RSS feeds   — CalMatters, Capitol Weekly, KQED, LAist, Google News (no key needed)
    NewsAPI     — requires NEWSAPI_KEY in .env (free tier: 100 req/day, 1-month lookback)
    X API v2    — STUB: requires X_BEARER_TOKEN ($100/month Basic tier)
                  See _scan_x_api() for activation instructions

Requires:
    feedparser, requests, python-dateutil (all in requirements.txt)
    Optional: NEWSAPI_KEY, X_BEARER_TOKEN in .env
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env", override=True)

import feedparser
import requests
from dateutil import parser as dateparser

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = _PROJECT_ROOT
BILLS_FILE   = PROJECT_ROOT / "data" / "bills" / "tracked_bills.json"
OUTPUT_DIR   = PROJECT_ROOT / "data" / "media"
OUTPUT_FILE  = OUTPUT_DIR / "media_digest.json"

# ---------------------------------------------------------------------------
# RSS feed sources
# ---------------------------------------------------------------------------
#
# Only feeds confirmed reachable as of 2026-02-25.
# LA Times (feeds.latimes.com) and Sacramento Bee are currently broken/blocked.
# Google News search RSS provides broad coverage as a catch-all.
#
# Feed format: {"name": display label, "url": RSS URL, "weight": relevance multiplier}
# weight > 1.0 = authoritative CA policy source (bumps relevance score)

RSS_FEEDS = [
    {
        "name":   "CalMatters — Housing",
        "url":    "https://calmatters.org/housing/feed/",
        "weight": 1.5,   # Primary CA policy publication, housing beat — highest authority
    },
    {
        "name":   "Capitol Weekly",
        "url":    "https://capitolweekly.net/feed/",
        "weight": 1.4,   # Sacramento insider publication
    },
    {
        "name":   "KQED News",
        "url":    "https://www.kqed.org/news/feed/",
        "weight": 1.1,
    },
    {
        "name":   "LAist",
        "url":    "https://laist.com/rss",
        "weight": 1.0,
    },
    {
        "name":   "Google News — CA Housing (recent)",
        "url":    (
            "https://news.google.com/rss/search"
            "?q=california+housing+local+control+zoning+when:7d"
            "&hl=en-US&gl=US&ceid=US:en"
        ),
        "weight": 1.0,
        # NOTE: `when:7d` scopes Google News to the past 7 days.
        # Without it, Google News returns historically popular results (months old).
    },
    {
        "name":   "Google News — CA Preemption (recent)",
        "url":    (
            "https://news.google.com/rss/search"
            "?q=california+zoning+preemption+city+council+when:7d"
            "&hl=en-US&gl=US&ceid=US:en"
        ),
        "weight": 1.0,
    },
    {
        "name":   "Google News — Local Control (recent)",
        "url":    (
            "https://news.google.com/rss/search"
            "?q=%22local+control%22+california+housing+when:7d"
            "&hl=en-US&gl=US&ceid=US:en"
        ),
        "weight": 1.1,   # Quoted phrase match — more precise signal
    },
]

# ---------------------------------------------------------------------------
# Keyword and bill-number matching
# ---------------------------------------------------------------------------

# Core topic keywords — presence in title/summary boosts relevance
_TOPIC_KEYWORDS = [
    "local control",
    "zoning",
    "preemption",
    "housing mandate",
    "discretionary review",
    "CEQA",
    "impact fee",
    "ADU",
    "accessory dwelling",
    "by-right",
    "ministerial",
    "density bonus",
    "RHNA",
    "transit-oriented",
    "upzoning",
    "infrastructure cost",
    "affordable housing",
    "city council",
    "planning commission",
    "general plan",
]

# Bill number pattern — matches AB1751, SB 9, ACA 10, SCR 44, etc.
_BILL_PATTERN = re.compile(
    r"\b(AB|SB|ACA|SCA|ACR|SCR|HR|SR)\s*(\d{1,4})\b",
    re.IGNORECASE,
)


def _normalize_bill_number(match: re.Match) -> str:
    """Normalize e.g. 'AB 1751' → 'AB1751', 'sb 9' → 'SB9'."""
    return f"{match.group(1).upper()}{match.group(2)}"


def _extract_bill_mentions(text: str, tracked: set[str]) -> list[str]:
    """Return bill numbers mentioned in text that are in the tracked set."""
    found = set()
    for m in _BILL_PATTERN.finditer(text or ""):
        bn = _normalize_bill_number(m)
        if bn in tracked:
            found.add(bn)
    return sorted(found)


def _score_article(title: str, summary: str, bill_mentions: list[str], weight: float) -> float:
    """Compute a relevance score [0.0–5.0] for an article.

    Scoring:
        +1.0 per tracked bill number mentioned (high signal)
        +0.3 per topic keyword found in title
        +0.1 per topic keyword found in summary only
        × source weight (1.0–1.5)

    Articles with score < 0.3 after weighting are considered off-topic.
    """
    score = 0.0
    score += len(bill_mentions) * 1.0

    text_lower   = (title or "").lower()
    summary_lower = (summary or "").lower()

    for kw in _TOPIC_KEYWORDS:
        kw_lower = kw.lower()
        if kw_lower in text_lower:
            score += 0.3
        elif kw_lower in summary_lower:
            score += 0.1

    return round(min(score * weight, 5.0), 2)


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

def _parse_date(date_str: str | None) -> str | None:
    """Parse a date string from any common format → ISO date string (YYYY-MM-DD).

    Returns None if the string is empty or unparseable.
    """
    if not date_str:
        return None
    try:
        dt = dateparser.parse(date_str, ignoretz=True)
        return dt.date().isoformat() if dt else None
    except Exception:
        return None


def _is_within_lookback(date_str: str | None, lookback_days: int) -> bool:
    """Return True if date_str (ISO YYYY-MM-DD) falls within the lookback window."""
    if not date_str:
        return True   # Include undated articles rather than silently dropping them
    try:
        article_date = date.fromisoformat(date_str)
        cutoff       = date.today() - timedelta(days=lookback_days)
        return article_date >= cutoff
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Module 1: RSS scanner
# ---------------------------------------------------------------------------

def _scan_rss_feeds(
    feeds: list[dict],
    tracked_bills: set[str],
    lookback_days: int,
    min_score: float = 0.3,
) -> list[dict]:
    """Fetch and filter all RSS feeds.

    Returns a list of article dicts sorted by relevance_score descending.
    Handles feed errors gracefully — a dead feed logs a warning and continues.
    """
    articles: list[dict] = []

    for feed_cfg in feeds:
        name   = feed_cfg["name"]
        url    = feed_cfg["url"]
        weight = feed_cfg.get("weight", 1.0)

        try:
            log.info(f"   RSS ← {name}")
            parsed = feedparser.parse(url)

            if parsed.bozo and not parsed.entries:
                log.warning(f"      ⚠ {name}: feed parse error — {parsed.bozo_exception}")
                continue

            feed_count = 0
            for entry in parsed.entries:
                title    = entry.get("title",   "")
                summary  = entry.get("summary", "") or entry.get("description", "")
                link     = entry.get("link",    "")
                pub_date = _parse_date(
                    entry.get("published") or
                    entry.get("updated")   or
                    entry.get("created")
                )

                if not _is_within_lookback(pub_date, lookback_days):
                    continue

                # Strip HTML tags from summary for cleaner text matching
                clean_summary = re.sub(r"<[^>]+>", " ", summary).strip()

                bill_mentions = _extract_bill_mentions(
                    f"{title} {clean_summary}", tracked_bills
                )
                score = _score_article(title, clean_summary, bill_mentions, weight)

                if score < min_score:
                    continue

                articles.append({
                    "source":          name,
                    "source_type":     "rss",
                    "title":           title,
                    "url":             link,
                    "published":       pub_date,
                    "summary":         clean_summary[:500],
                    "bill_mentions":   bill_mentions,
                    "relevance_score": score,
                })
                feed_count += 1

            log.info(f"      → {feed_count} relevant article(s)")

        except Exception as exc:
            log.warning(f"      ⚠ {name}: failed to fetch — {exc}")
            continue

    # Sort by relevance then recency
    articles.sort(key=lambda a: (-a["relevance_score"], a["published"] or ""))
    return articles


# ---------------------------------------------------------------------------
# Module 2: NewsAPI scanner
# ---------------------------------------------------------------------------
#
# Free tier: 100 requests/day, articles up to 1 month old, rate limited.
# Requires NEWSAPI_KEY in .env — get a free key at https://newsapi.org/register
#
# Paid tiers unlock older archives and higher rate limits. For CSF usage the
# free tier is sufficient (1 request per weekly run).

_NEWSAPI_ENDPOINT = "https://newsapi.org/v2/everything"

_NEWSAPI_QUERIES = [
    "california housing local control",
    "california zoning preemption city",
    "california housing bill legislature",
]


def _scan_newsapi(
    tracked_bills: set[str],
    lookback_days: int,
    min_score: float = 0.3,
) -> tuple[list[dict], str]:
    """Query NewsAPI for recent CA housing policy coverage.

    Returns (articles, status) where status is one of:
        "ok"             — results returned
        "not_configured" — NEWSAPI_KEY not set (skip silently)
        "error"          — API call failed (logged, returns empty list)
    """
    api_key = os.environ.get("NEWSAPI_KEY", "").strip()
    if not api_key:
        log.info("   NewsAPI: NEWSAPI_KEY not set — skipping")
        return [], "not_configured"

    from_date = (date.today() - timedelta(days=min(lookback_days, 30))).isoformat()
    articles:   list[dict] = []
    seen_urls:  set[str]   = set()

    for query in _NEWSAPI_QUERIES:
        params = {
            "q":          query,
            "from":       from_date,
            "language":   "en",
            "sortBy":     "relevancy",
            "pageSize":   20,
            "apiKey":     api_key,
        }
        try:
            resp = requests.get(_NEWSAPI_ENDPOINT, params=params, timeout=15)

            if resp.status_code == 401:
                log.error("   NewsAPI: invalid API key — check NEWSAPI_KEY in .env")
                return [], "error"
            if resp.status_code == 426:
                log.warning("   NewsAPI: free tier limit reached — upgrade plan for more")
                return articles, "rate_limited"

            resp.raise_for_status()
            data = resp.json()

            for item in data.get("articles", []):
                url = item.get("url", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title    = item.get("title",       "") or ""
                summary  = item.get("description", "") or ""
                pub_date = _parse_date(item.get("publishedAt"))

                if not _is_within_lookback(pub_date, lookback_days):
                    continue

                bill_mentions = _extract_bill_mentions(
                    f"{title} {summary}", tracked_bills
                )
                score = _score_article(title, summary, bill_mentions, weight=1.0)

                if score < min_score:
                    continue

                articles.append({
                    "source":          item.get("source", {}).get("name", "NewsAPI"),
                    "source_type":     "newsapi",
                    "title":           title,
                    "url":             url,
                    "published":       pub_date,
                    "summary":         summary[:500],
                    "bill_mentions":   bill_mentions,
                    "relevance_score": score,
                })

        except requests.RequestException as exc:
            log.warning(f"   NewsAPI: request failed for '{query}' — {exc}")
            continue

    log.info(f"   NewsAPI → {len(articles)} relevant article(s)")
    articles.sort(key=lambda a: (-a["relevance_score"], a["published"] or ""))
    return articles, "ok"


# ---------------------------------------------------------------------------
# Module 3: X API v2 — STUB
# ---------------------------------------------------------------------------
#
# STATUS: Stub — not functional until X_BEARER_TOKEN is configured.
#
# ACTIVATION:
#   1. Sign up for X Developer account: https://developer.twitter.com/en/portal
#   2. Create a project and app — choose "Read" permissions
#   3. Copy the Bearer Token from the app dashboard
#   4. Add to .env:  X_BEARER_TOKEN=your_bearer_token_here
#   5. Uncomment the implementation block in _scan_x_api() below
#
# COST:
#   Free tier  — write-only access, no search (useless here)
#   Basic tier — $100/month → 10k tweet reads/month, recent search available ✓
#   Pro tier   — $5,000/month → full archive, higher limits
#
#   For CSF's use case (weekly scan, 20–50 results), Basic tier ($100/month)
#   is sufficient. Evaluate after the content program is established.
#
# SEARCH QUERY SHAPE (when activated):
#   (#LocalControl OR #CaliforniaHousing OR "AB1751" OR "SB9") lang:en -is:retweet
#
# ENDPOINT:
#   GET https://api.twitter.com/2/tweets/search/recent
#   Docs: https://developer.twitter.com/en/docs/twitter-api/tweets/search/api-reference

_X_SEARCH_ENDPOINT = "https://api.twitter.com/2/tweets/search/recent"

_X_HASHTAGS = [
    "#LocalControl",
    "#CaliforniaHousing",
    "#CAHousing",
    "#ZoningMatters",
    "#WhoDecides",
    "#CALeg",
]


def _scan_x_api(
    tracked_bills: set[str],
    lookback_days: int,
) -> tuple[list[dict], str]:
    """X API v2 recent tweet search.

    Returns (posts, status) where status is one of:
        "not_configured" — X_BEARER_TOKEN not set (expected in MVP)
        "ok"             — results returned
        "error"          — API call failed

    To activate: follow the instructions in the module docstring above,
    then uncomment the implementation block below.
    """
    bearer_token = os.environ.get("X_BEARER_TOKEN", "").strip()

    if not bearer_token:
        log.info("   X API:   X_BEARER_TOKEN not set — skipping (stub)")
        return [], "not_configured"

    # ── Stub guard ────────────────────────────────────────────────────────────
    # Remove this block and uncomment the implementation below to activate.
    log.warning("   X API:   X_BEARER_TOKEN is set but implementation is stubbed.")
    log.warning("            Uncomment the implementation block in _scan_x_api().")
    return [], "stub_inactive"
    # ─────────────────────────────────────────────────────────────────────────

    # ── Implementation (uncomment when ready) ─────────────────────────────────
    #
    # # Build query: top bill numbers OR key hashtags, English, no retweets
    # top_bills  = sorted(tracked_bills)[:10]   # Limit query length
    # bill_terms = " OR ".join(f'"{bn}"' for bn in top_bills)
    # hash_terms = " OR ".join(_X_HASHTAGS)
    # query      = f"({hash_terms} OR {bill_terms}) lang:en -is:retweet"
    #
    # # X API allows max 7-day lookback on recent search (Basic tier)
    # lookback_capped = min(lookback_days, 7)
    # start_time = (
    #     datetime.now(timezone.utc) - timedelta(days=lookback_capped)
    # ).strftime("%Y-%m-%dT%H:%M:%SZ")
    #
    # params = {
    #     "query":        query,
    #     "max_results":  50,
    #     "start_time":   start_time,
    #     "tweet.fields": "created_at,text,author_id,public_metrics",
    #     "expansions":   "author_id",
    #     "user.fields":  "username,name,verified",
    # }
    # headers = {"Authorization": f"Bearer {bearer_token}"}
    #
    # try:
    #     resp = requests.get(
    #         _X_SEARCH_ENDPOINT, params=params, headers=headers, timeout=15
    #     )
    #     if resp.status_code == 401:
    #         log.error("   X API: authentication failed — check X_BEARER_TOKEN")
    #         return [], "error"
    #     if resp.status_code == 403:
    #         log.error("   X API: access denied — Basic tier ($100/mo) required for search")
    #         return [], "error"
    #     resp.raise_for_status()
    #     data = resp.json()
    # except requests.RequestException as exc:
    #     log.warning(f"   X API: request failed — {exc}")
    #     return [], "error"
    #
    # # Build user lookup map from expansions
    # users = {
    #     u["id"]: u
    #     for u in data.get("includes", {}).get("users", [])
    # }
    #
    # posts = []
    # for tweet in data.get("data", []):
    #     user         = users.get(tweet.get("author_id", ""), {})
    #     username     = user.get("username", "unknown")
    #     text         = tweet.get("text", "")
    #     tweet_id     = tweet.get("id", "")
    #     created_at   = tweet.get("created_at", "")
    #     metrics      = tweet.get("public_metrics", {})
    #     bill_mentions = _extract_bill_mentions(text, tracked_bills)
    #
    #     posts.append({
    #         "source":          "x_api",
    #         "source_type":     "x",
    #         "text":            text,
    #         "author":          username,
    #         "author_name":     user.get("name", ""),
    #         "published":       _parse_date(created_at),
    #         "url":             f"https://x.com/{username}/status/{tweet_id}",
    #         "bill_mentions":   bill_mentions,
    #         "likes":           metrics.get("like_count", 0),
    #         "reposts":         metrics.get("retweet_count", 0),
    #         "relevance_score": _score_article(text, "", bill_mentions, weight=1.0),
    #     })
    #
    # posts.sort(key=lambda p: (-p["relevance_score"], -(p["likes"] + p["reposts"])))
    # log.info(f"   X API  → {len(posts)} relevant post(s)")
    # return posts, "ok"
    # ─────────────────────────────────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------

def _build_digest(
    articles: list[dict],
    x_posts:  list[dict],
    x_status: str,
    newsapi_status: str,
    tracked_bills: set[str],
    lookback_days: int,
    sources_scanned: list[str],
    max_articles: int = 20,
) -> dict:
    """Assemble the final media_digest.json structure."""
    # Deduplicate articles by URL
    seen: set[str] = set()
    deduped: list[dict] = []
    for a in articles:
        key = a.get("url") or a.get("title", "")
        if key not in seen:
            seen.add(key)
            deduped.append(a)

    deduped = deduped[:max_articles]

    # Tally top mentioned bills
    bill_counts: dict[str, int] = {}
    for a in deduped:
        for bn in a.get("bill_mentions", []):
            bill_counts[bn] = bill_counts.get(bn, 0) + 1
    for p in x_posts:
        for bn in p.get("bill_mentions", []):
            bill_counts[bn] = bill_counts.get(bn, 0) + 1

    top_bills = sorted(bill_counts, key=lambda b: -bill_counts[b])[:10]

    return {
        "generated":       datetime.now().isoformat(timespec="seconds"),
        "lookback_days":   lookback_days,
        "articles":        deduped,
        "x_posts":         x_posts,
        "summary": {
            "total_articles":    len(deduped),
            "total_x_posts":     len(x_posts),
            "top_bill_mentions": top_bills,
            "bill_mention_counts": bill_counts,
            "sources_scanned":   sources_scanned,
            "newsapi_status":    newsapi_status,
            "x_status":          x_status,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scan news and social media for CA housing policy coverage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Scan all sources, write media_digest.json (default)
  python agents/media/media_scanner.py

  # Shorter lookback window (e.g. mid-week refresh)
  python agents/media/media_scanner.py --lookback 3

  # Scan but don't write output (preview mode)
  python agents/media/media_scanner.py --dry-run

  # Use a different bill data source
  python agents/media/media_scanner.py --bills data/bills/tracked_bills.json

optional env vars:
  NEWSAPI_KEY       Free at newsapi.org — adds NewsAPI results
  X_BEARER_TOKEN    X API Basic tier ($100/mo) — see _scan_x_api() stub
        """,
    )
    p.add_argument(
        "--bills", type=Path, default=None,
        help="Path to tracked_bills.json (default: data/bills/tracked_bills.json)",
    )
    p.add_argument(
        "--lookback", type=int, default=7,
        help="Days of news to scan (default: 7)",
    )
    p.add_argument(
        "--min-score", type=float, default=0.3,
        help="Minimum relevance score to include an article (default: 0.3)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Scan and print summary but do not write media_digest.json",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    print("\n  CSF Media Scanner")
    print("  " + "─" * 30)

    # ── Load tracked bill numbers ────────────────────────────────────────────
    bills_path = args.bills or BILLS_FILE
    log.info(f"→ Loading {bills_path.name}...")
    data         = json.loads(bills_path.read_text())
    tracked_bills = set(data["bills"].keys())
    log.info(f"   {len(tracked_bills)} tracked bill numbers loaded")

    # ── Scan RSS feeds ───────────────────────────────────────────────────────
    log.info(f"→ Scanning {len(RSS_FEEDS)} RSS feeds (lookback: {args.lookback} days)...")
    rss_articles = _scan_rss_feeds(
        RSS_FEEDS, tracked_bills, args.lookback, args.min_score
    )

    # ── Scan NewsAPI ─────────────────────────────────────────────────────────
    log.info("→ Scanning NewsAPI...")
    newsapi_articles, newsapi_status = _scan_newsapi(
        tracked_bills, args.lookback, args.min_score
    )

    # ── Scan X API (stub) ────────────────────────────────────────────────────
    log.info("→ Scanning X API...")
    x_posts, x_status = _scan_x_api(tracked_bills, args.lookback)

    # ── Combine and deduplicate ───────────────────────────────────────────────
    all_articles  = rss_articles + newsapi_articles
    all_articles.sort(key=lambda a: (-a["relevance_score"], a.get("published") or ""))
    sources_scanned = [f["name"] for f in RSS_FEEDS]
    if newsapi_status == "ok":
        sources_scanned.append("NewsAPI")

    digest = _build_digest(
        articles        = all_articles,
        x_posts         = x_posts,
        x_status        = x_status,
        newsapi_status  = newsapi_status,
        tracked_bills   = tracked_bills,
        lookback_days   = args.lookback,
        sources_scanned = sources_scanned,
    )

    # ── Print summary ─────────────────────────────────────────────────────────
    s = digest["summary"]
    print(f"\n  Articles found:  {s['total_articles']}")
    print(f"  X posts found:   {s['total_x_posts']}  ({x_status})")
    print(f"  NewsAPI status:  {newsapi_status}")
    if s["top_bill_mentions"]:
        print(f"  Top bills:       {', '.join(s['top_bill_mentions'][:5])}")
    else:
        print("  Top bills:       (none mentioned by name in coverage)")

    if digest["articles"]:
        print(f"\n  Top {min(5, len(digest['articles']))} articles by relevance:")
        for a in digest["articles"][:5]:
            bills_str = f"  [{', '.join(a['bill_mentions'])}]" if a["bill_mentions"] else ""
            print(f"    {a['relevance_score']:.1f}  {a['source']:20s}  {a['title'][:60]}{bills_str}")

    # ── Write output ──────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"\n  (Dry-run — not writing {OUTPUT_FILE.name})\n")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  ✓ Written to: {OUTPUT_FILE.relative_to(PROJECT_ROOT)}\n")


if __name__ == "__main__":
    main()
