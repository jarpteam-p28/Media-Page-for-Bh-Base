#!/usr/bin/env python3
"""
Facebook cross-post -- Stage 6, stub.

Posts a link to a newly-published article on your Facebook Page, using
Meta's official Graph API. This is a STUB: it will not run successfully
until you complete the one-time setup below, which is account/app setup on
Meta's side, not something I can do from code.

ONE-TIME SETUP (does this once, outside of code):
  1. Your Facebook Page must be linked to a Meta Business account.
  2. Create a Meta App at developers.facebook.com, add the "Pages" product.
  3. Generate a Page Access Token for your Page with the `pages_manage_posts`
     permission (a long-lived token, not the 1-hour default one).
  4. If you want this to run from GitHub Actions unattended, store that
     token as a repo secret: FB_PAGE_ACCESS_TOKEN, and the Page's numeric ID
     as FB_PAGE_ID.
  5. Apps posting on a schedule/automatically may need Meta's "App Review"
     for the pages_manage_posts permission once you're off a Page you admin
     yourself in developer mode -- this is a Meta policy gate, not a
     technical one.

WHAT THIS SCRIPT DOES once the above is done:
  Takes a published article's title + URL + a short excerpt and posts it to
  the Page's feed via POST /{page-id}/feed.

Usage:
  python3 scripts/crosspost_facebook.py content/published/<niche>/<file>.md
"""
import os
import pathlib
import sys

import requests
import yaml

GRAPH_API_VERSION = "v21.0"
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://REPLACE-WITH-YOUR-DOMAIN.example")


def post_to_facebook(message: str, link: str) -> dict:
    page_id = os.environ["FB_PAGE_ID"]
    token = os.environ["FB_PAGE_ACCESS_TOKEN"]
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{page_id}/feed"
    resp = requests.post(url, data={"message": message, "link": link, "access_token": token})
    resp.raise_for_status()
    return resp.json()


def run():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    path = pathlib.Path(sys.argv[1])
    raw = path.read_text()
    _, fm_text, _ = raw.split("---", 2)
    fm = yaml.safe_load(fm_text)

    niche = path.parent.name
    slug = fm.get("slug", path.stem)
    url = f"{SITE_BASE_URL}/{niche}/{slug}/"
    message = fm.get("title", "New article")

    if "FB_PAGE_ACCESS_TOKEN" not in os.environ:
        print("[dry-run] Would post to Facebook:")
        print(f"  message: {message}")
        print(f"  link: {url}")
        print("Set FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN to actually post.")
        return

    result = post_to_facebook(message, url)
    print(f"[ok] posted: {result}")


if __name__ == "__main__":
    run()
