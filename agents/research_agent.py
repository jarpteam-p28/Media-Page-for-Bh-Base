#!/usr/bin/env python3
"""
Research agent -- Stage 1 of the pipeline.

For every niche in config/niches.yaml, pulls currently-trending items from:
  - Google News RSS (per search term) -- free, no key needed
  - Reddit's public .json listing endpoint (per subreddit, "hot") -- free, no key needed
  - config/story_picks.yaml -- stories Pablo hand-picks (Reddit links, TikTok phrases,
    article links). These always go to the top of the list.

Also collects what people are searching and watching right now, used only as a
boost (a news story matching a trending search is preferred), never as a story
on its own since those lists are mostly sports scores and celebrities:
  - Google Trends daily trending searches (US) -- free, no key needed
  - YouTube most-popular videos (US) -- only if YOUTUBE_API_KEY is set; uses 1 unit
    of the free 10,000/day YouTube Data API quota per run

Writes one JSON file per niche to content/drafts/_candidates/<slug>.json with
a list of candidate stories: {title, url, source, published, snippet} plus the
shared `trending` list.

This step does NOT write articles. It only surfaces raw candidates for the
writer agent (agents/writer_agent.py) to pick from and draft. Keeping these
separate means a human can also just open the candidates file and hand-pick
what gets written, instead of trusting the writer agent's own pick.

No external dependencies beyond `requests` and `pyyaml` and the stdlib.
"""
import json
import os
import pathlib
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
NICHES_FILE = ROOT / "config" / "niches.yaml"
PICKS_FILE = ROOT / "config" / "story_picks.yaml"
OUT_DIR = ROOT / "content" / "drafts" / "_candidates"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; media-research-agent/1.0)"}


def google_news_rss(term: str, max_items: int = 8):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": term, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall(".//item")[:max_items]:
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "url": (item.findtext("link") or "").strip(),
                "source": (item.findtext("source") or "Google News").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
                "snippet": (item.findtext("description") or "").strip(),
                "via": f'google_news:"{term}"',
            }
        )
    return items


def reddit_hot(subreddit: str, max_items: int = 8):
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={max_items}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    items = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied"):
            continue
        items.append(
            {
                "title": d.get("title", ""),
                "url": "https://reddit.com" + d.get("permalink", ""),
                "source": f"r/{subreddit}",
                "published": "",
                "snippet": (d.get("selftext") or "")[:300],
                "via": f"reddit:r/{subreddit}",
                "upvotes": d.get("ups"),
                "num_comments": d.get("num_comments"),
            }
        )
    return items


def story_picks(slug: str):
    # Hand-picked stories for this niche from config/story_picks.yaml. Each pick needs a
    # `title` (a headline or a phrase like a TikTok trend) and may have a `url` and `note`.
    if not PICKS_FILE.exists():
        return []
    picks = (yaml.safe_load(PICKS_FILE.read_text()) or {}).get("picks") or []
    return [
        {
            "title": p["title"].strip(),
            "url": (p.get("url") or "").strip(),
            "source": "Story pick",
            "published": "",
            "snippet": (p.get("note") or "").strip(),
            "via": "story_picks",
            "pick": True,
        }
        for p in picks
        if p.get("niche") == slug and (p.get("title") or "").strip()
    ]


def google_trends(max_items: int = 25):
    resp = requests.get("https://trends.google.com/trending/rss?geo=US", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    return [(item.findtext("title") or "").strip() for item in root.findall(".//item")[:max_items]]


def youtube_trending(max_items: int = 25):
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return []
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "snippet", "chart": "mostPopular", "regionCode": "US", "maxResults": max_items, "key": key},
        timeout=20,
    )
    resp.raise_for_status()
    return [v["snippet"]["title"] for v in resp.json().get("items", [])]


def run():
    niches = yaml.safe_load(NICHES_FILE.read_text())["niches"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    trending = []
    for name, source in (("google trends", google_trends), ("youtube", youtube_trending)):
        try:
            trending.extend({"term": term, "via": name} for term in source() if term)
        except Exception as e:  # a boost signal, never worth failing the run
            print(f"[warn] {name} failed: {e}", file=sys.stderr)
    print(f"[ok] {len(trending)} trending searches/videos collected")

    for niche in niches:
        slug = niche["slug"]
        candidates = story_picks(slug)

        for term in niche.get("search_terms", []):
            try:
                candidates.extend(google_news_rss(term))
            except Exception as e:  # network hiccups shouldn't kill the whole run
                print(f"[warn] google news failed for {slug}/{term!r}: {e}", file=sys.stderr)
            time.sleep(1)  # be polite

        for sub in niche.get("subreddits", []):
            try:
                candidates.extend(reddit_hot(sub))
            except Exception as e:
                print(f"[warn] reddit failed for {slug}/r/{sub}: {e}", file=sys.stderr)
            time.sleep(1)

        # de-dupe by URL (picks may have no URL, so those are de-duped by title)
        seen = set()
        deduped = []
        for c in candidates:
            key = c["url"] or c["title"]
            if key and key not in seen:
                seen.add(key)
                deduped.append(c)

        out_path = OUT_DIR / f"{slug}.json"
        out_path.write_text(json.dumps({"niche": slug, "generated_at": time.time(),
                                         "candidates": deduped, "trending": trending}, indent=2))
        print(f"[ok] {slug}: {len(deduped)} candidates -> {out_path}")


if __name__ == "__main__":
    run()
