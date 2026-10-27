#!/usr/bin/env python3
"""
Image agent -- runs right after the writer agent.

For every draft in content/drafts/*/*.md that has no `image` yet, searches for a
free photo using the draft's `image_query` (or its title), downloads it once,
shrinks it to web size, and saves it as content/images/<niche>/<draft-name>.jpg.
The photo is stored with the site rather than linked from the photo site, so
pages load fast and never break when the other site is slow or rate-limits us.

These fields are written into the draft's frontmatter:

  image             /images/<niche>/<draft-name>.jpg (the copy served by the site)
  image_credit      photographer / creator name
  image_credit_url  link to the photo's page (required by the licenses)
  image_license     e.g. "Pexels License" or "CC BY 2.0"
  image_source      where the original file came from (for the record)

Sources, in order:
  1. Pexels (better photos) -- only if PEXELS_API_KEY is set. Free key at
     https://www.pexels.com/api/ ; add it as a repo secret named PEXELS_API_KEY.
  2. Wikimedia Commons (commons.wikimedia.org) -- free, no key. Only CC0,
     public-domain, CC BY and CC BY-SA photos (all allow commercial use and
     modification), landscape, at least 1000px wide.
  3. Openverse (api.openverse.org) -- free, no key. Only images whose license
     allows commercial use AND modification are requested, so resizing and
     cropping are fine. Often unreachable from GitHub's runners, so it's last.

If nothing usable is found the draft is left without an image; the site shows a
plain colored block instead. You can always swap the photo by hand in the PR.

Usage: python3 agents/image_agent.py            (all drafts missing an image)
       python3 agents/image_agent.py <file.md>  (one file)
"""
import io
import os
import pathlib
import re
import sys
import time

import requests
import yaml
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent
DRAFTS_DIR = ROOT / "content" / "drafts"
IMAGES_DIR = ROOT / "content" / "images"
# Wikimedia (a common Openverse source) asks for a descriptive User-Agent with contact info.
HEADERS = {"User-Agent": "bh-base-media-image-agent/1.0 (https://github.com/jarpteam-p28/media-page-for-bh-base)"}
MAX_WIDTH = 1280


def split_frontmatter(raw: str):
    if not raw.startswith("---"):
        return None, raw
    _, fm_text, body = raw.split("---", 2)
    return yaml.safe_load(fm_text) or {}, body


def join_frontmatter(fm: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---" + body


def from_pexels(query: str):
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return []
    resp = requests.get(
        "https://api.pexels.com/v1/search",
        params={"query": query, "orientation": "landscape", "per_page": 3},
        headers={**HEADERS, "Authorization": key},
        timeout=20,
    )
    resp.raise_for_status()
    return [
        {
            "download": p["src"]["large2x"],
            "image_credit": p["photographer"],
            "image_credit_url": p["url"],
            "image_license": "Pexels License",
        }
        for p in resp.json().get("photos", [])
    ]


COMMONS_LICENSE_OK = re.compile(r"^(cc0|public domain|pd|cc[- ]by(-sa)?[- ]?\d)", re.IGNORECASE)
# Skip pictures of identifiable or famous people (a real person must never illustrate a
# story they have nothing to do with) and things that aren't photos (charts, maps, logos).
COMMONS_SKIP = re.compile(
    r"portrait|president|politician|minister|parliament|governor|senator|mayor|official photo|"
    r"celebrit|actor|actress|singer|musician|athlete|footballer|player|people of|people by name|"
    r"chart|graph|diagram|map|logo|infographic|screenshot|poster|flag|coat of arms|signature",
    re.IGNORECASE,
)


def from_wikimedia(query: str):
    resp = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
            "gsrlimit": 10, "gsrsearch": f"{query} filetype:bitmap",
            "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime|commonmetadata", "iiurlwidth": MAX_WIDTH,
        },
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    pages = sorted(resp.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    found = []
    for p in pages:
        info = p["imageinfo"][0]
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "")
        # Landscape photos only: they fill the site's wide photo slots without awkward crops.
        if not COMMONS_LICENSE_OK.match(license_name) or info.get("width", 0) < 1000 or info["width"] < info["height"] * 1.2:
            continue
        if info.get("mime") != "image/jpeg":  # real photos are JPEGs; charts and scans usually aren't
            continue
        # Real photos carry the camera's make/model; charts, infographics and renders don't.
        if not any(m.get("name") in ("Make", "Model") for m in info.get("commonmetadata", [])):
            continue
        restrictions = meta.get("Restrictions", {}).get("value", "")
        about = " ".join([p.get("title", ""), meta.get("Categories", {}).get("value", ""),
                          re.sub(r"<[^>]+>", " ", meta.get("ImageDescription", {}).get("value", ""))])
        if "personality" in restrictions or "trademark" in restrictions or COMMONS_SKIP.search(about):
            continue
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "Unknown"
        found.append({
            "download": info["thumburl"],
            "image_credit": artist[:80],
            "image_credit_url": info["descriptionurl"],
            "image_license": license_name,
        })
    return found


def from_openverse(query: str):
    resp = requests.get(
        "https://api.openverse.org/v1/images/",
        params={
            "q": query,
            "license_type": "commercial,modification",
            "aspect_ratio": "wide",
            "size": "large",
            "mature": "false",
            "page_size": 5,
        },
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    found = []
    for r in resp.json().get("results", []):
        if not r.get("url"):
            continue
        version = f" {r['license_version']}" if r.get("license_version") else ""
        found.append({
            "download": r["url"],
            "image_credit": r.get("creator") or "Unknown",
            "image_credit_url": r.get("foreign_landing_url") or r["url"],
            "image_license": "Public domain" if r["license"] in ("cc0", "pdm")
                             else f"CC {r['license'].upper()}{version}",
        })
    return found


def broader_queries(query: str):
    # A specific search ("tart cherry juice glass") often finds nothing, so fall
    # back to shorter versions of it ("tart cherry", then "glass").
    words = query.split()
    candidates = [query, " ".join(words[:2]), words[-1] if words else ""]
    return [q for i, q in enumerate(candidates) if q and q not in candidates[:i]]


def download_resized(url: str, dest: pathlib.Path) -> bool:
    for attempt in range(3):
        resp = requests.get(url, headers=HEADERS, timeout=60)
        if resp.status_code == 429:  # rate-limited: wait as asked (capped), then retry
            time.sleep(min(int(resp.headers.get("retry-after", "5") or 5), 20))
            continue
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        img = img.convert("RGB")
        img.thumbnail((MAX_WIDTH, MAX_WIDTH))
        if img.width < 600:  # too small to look good as a lead photo
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=80, optimize=True, progressive=True)
        return True
    return False


def add_image(path: pathlib.Path):
    fm, body = split_frontmatter(path.read_text())
    if fm is None or fm.get("image"):
        return
    query = fm.get("image_query") or fm.get("title", "")
    dest = IMAGES_DIR / path.parent.name / f"{path.stem}.jpg"
    for q in broader_queries(query):
        for source in (from_pexels, from_wikimedia, from_openverse):
            try:
                options = source(q)
            except Exception as e:  # a photo is nice to have, never worth failing the run
                print(f"[warn] {source.__name__} failed for {q!r}: {e}", file=sys.stderr)
                continue
            for opt in options:
                try:
                    ok = download_resized(opt["download"], dest)
                except Exception as e:
                    print(f"[warn] could not download {opt['download']}: {e}", file=sys.stderr)
                    continue
                if not ok:
                    continue
                fm.update({
                    "image": f"/images/{path.parent.name}/{dest.name}",
                    "image_credit": opt["image_credit"],
                    "image_credit_url": opt["image_credit_url"],
                    "image_license": opt["image_license"],
                    "image_source": opt["download"],
                })
                path.write_text(join_frontmatter(fm, body))
                print(f"[ok] photo for {path.name}: {opt['image_credit']} ({opt['image_license']})")
                return
    print(f"[skip] no usable photo found for {path.name} ({query!r})")


def run():
    targets = [pathlib.Path(sys.argv[1])] if len(sys.argv) > 1 else sorted(DRAFTS_DIR.glob("*/*.md"))
    for path in targets:
        add_image(path)


if __name__ == "__main__":
    run()
