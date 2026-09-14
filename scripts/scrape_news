#!/usr/bin/env python3
"""
Pulls real financial/market news via Google News RSS feeds (free, no API
key or signup needed) and rewrites the "news" section of data.json.

WHY GOOGLE NEWS RSS
--------------------
RSS feeds are explicitly meant for automated/syndicated consumption - this
isn't scraping content a site doesn't want machines reading, it's the
intended distribution channel. Google News RSS search feeds aggregate
across many real publishers (Reuters, CNBC, Bloomberg, CNA, etc.) in one
place, and each entry keeps the real publisher name (from the <source>
tag) and a real link back to that publisher's own article.

This script does NOT reproduce full article bodies - only the headline
and the short summary snippet the RSS feed itself provides, same as any
legitimate news aggregator (Google News, Apple News, etc.) would show.
Respecting copyright here isn't optional, so the dashboard's news modal
links out to the original source for the full story rather than faking
one.

WHAT'S REAL VS. INFERRED
--------------------------
Real (straight from the RSS feed): headline, publish date, source
publisher name, link to the original article, short summary snippet.

Inferred by this script (RSS feeds don't provide these, so treat them as
illustrative, not authoritative):
  - "category" - which topic bucket a story goes in, based on which feed
    query surfaced it (see CATEGORY_FEEDS below). A single real-world
    story can plausibly fit more than one bucket; this is a best-effort
    single label, not an official classification.
  - "impact" (High/Medium/Low) - a rough keyword heuristic on the
    headline (see HIGH_IMPACT_WORDS/LOW_IMPACT_WORDS), not a rating from
    any actual source.

USAGE
-----
    pip install feedparser
    python scripts/scrape_news.py
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data.json"

# One Google News RSS search query per category this dashboard already
# uses. URL format is well-documented and stable:
#   https://news.google.com/rss/search?q=<query>&hl=<lang>&gl=<country>&ceid=<country>:<lang>
CATEGORY_FEEDS = {
    "US Markets": "US+stock+market+Wall+Street",
    "Commodities": "oil+gold+commodities+prices",
    "Singapore": "Singapore+stock+market+STI",
    "China": "China+economy+stock+market",
    "Monetary Policy": "federal+reserve+interest+rates+central+bank",
    "Global Markets": "global+stock+markets",
    "Bonds": "bond+yields+treasury",
    "FX": "currency+forex+dollar+exchange+rate",
    "Technology": "technology+stocks+earnings",
    "Hong Kong": "Hong+Kong+stock+market+Hang+Seng",
    "Japan": "Japan+economy+Nikkei+yen",
    "Geopolitics": "geopolitical+tensions+markets",
    "Europe": "European+markets+ECB",
    "Crypto": "bitcoin+cryptocurrency+market",
    "Trade": "trade+tariffs+global+trade",
    "US Economy": "US+economy+inflation+jobs+report",
    "US Politics": "US+politics+economic+policy",
}

HIGH_IMPACT_WORDS = [
    "crash", "plunge", "surge", "soar", "record high", "record low", "crisis",
    "war", "recession", "default", "collapse", "emergency", "sanctions",
    "attack", "rate hike", "rate cut", "plummet", "rally",
]
LOW_IMPACT_WORDS = ["steady", "little changed", "flat", "unchanged", "mixed", "holds"]


def guess_impact(title: str) -> str:
    t = title.lower()
    if any(w in t for w in HIGH_IMPACT_WORDS):
        return "High"
    if any(w in t for w in LOW_IMPACT_WORDS):
        return "Low"
    return "Medium"


def fetch_category(category: str, query: str, max_items: int = 8):
    import feedparser
    url = f"https://news.google.com/rss/search?q={query}&hl=en-SG&gl=SG&ceid=SG:en"
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:max_items]:
        published = entry.get("published_parsed")
        date_str = (datetime(*published[:6]).strftime("%Y-%m-%d")
                    if published else datetime.now().strftime("%Y-%m-%d"))
        source_info = entry.get("source")
        source = source_info.get("title") if source_info else None
        # Google News titles are usually "Headline - Publisher"; drop the
        # trailing " - Publisher" since we show the real source separately.
        title = entry.get("title", "").rsplit(" - ", 1)[0].strip()
        # NOTE: Google News RSS's <description> is just the title wrapped in
        # an <a> tag plus the publisher name again - not a genuine snippet -
        # so there's no real "summary" text to extract here. We deliberately
        # don't fabricate one; the dashboard links out to the real article
        # instead of pretending to show a preview of it.
        items.append({
            "date": date_str,
            "title": title,
            "category": category,
            "impact": guess_impact(title),
            "url": entry.get("link", ""),
            "source": source or "Google News",
        })
    return items


def main():
    all_news = []
    for category, query in CATEGORY_FEEDS.items():
        try:
            items = fetch_category(category, query)
            all_news.extend(items)
            print(f"{category}: {len(items)} articles")
        except Exception as e:
            print(f"FAILED {category}: {e}", file=sys.stderr)

    if not all_news:
        print("No news fetched from any feed - aborting without writing.", file=sys.stderr)
        sys.exit(1)

    # De-dupe by headline (the same real story often appears in more than
    # one category search), keep newest first.
    seen, deduped = set(), []
    for n in sorted(all_news, key=lambda x: x["date"], reverse=True):
        key = n["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)

    data = json.loads(DATA_PATH.read_text())
    data["news"] = deduped

    sgt = timezone(timedelta(hours=8))
    now = datetime.now(sgt)
    data["newsUpdatedAt"] = now.strftime("%d-%b-%Y %I:%M %p SGT")

    DATA_PATH.write_text(json.dumps(data))
    print(f"\nWrote {len(deduped)} real news articles to data.json")


if __name__ == "__main__":
    main()
