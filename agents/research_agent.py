#!/usr/bin/env python3
"""
Research agent -- Stage 1 of the pipeline.

For every niche in config/niches.yaml, pulls currently-trending items from:
  - Google News RSS (per search term) -- free, no key needed
  - Reddit's public .json listing endpoint (per subreddit, "hot") -- free, no key needed

Writes one JSON file per niche to content/drafts/_candidates/<slug>.json with
a list of candidate stories: {title, url, source, published, snippet}.

This step does NOT write articles. It only surfaces raw candidates for the
writer agent (agents/writer_agent.py) to pick from and draft. Keeping these
separate means a human can also just open the candidates file and hand-pick
what gets written, instead of trusting the writer agent's own pick.

No external dependencies beyond `requests` and `pyyaml` and the stdlib.
"""
import json
import pathlib
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
NICHES_FILE = ROOT / "config" / "niches.yaml"
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


def run():
    niches = yaml.safe_load(NICHES_FILE.read_text())["niches"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for niche in niches:
        slug = niche["slug"]
        candidates = []

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

        # de-dupe by URL
        seen = set()
        deduped = []
        for c in candidates:
            if c["url"] and c["url"] not in seen:
                seen.add(c["url"])
                deduped.append(c)

        out_path = OUT_DIR / f"{slug}.json"
        out_path.write_text(json.dumps({"niche": slug, "generated_at": time.time(),
                                         "candidates": deduped}, indent=2))
        print(f"[ok] {slug}: {len(deduped)} candidates -> {out_path}")


if __name__ == "__main__":
    run()
