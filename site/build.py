#!/usr/bin/env python3
"""
Static site builder -- Stage 5 of the pipeline.

Reads every approved article in content/published/*/*.md (markdown + YAML
frontmatter), renders it to HTML using site/template.html, and writes the
result into /dist (ready to deploy to Cloudflare Pages, Netlify, or GitHub
Pages -- all free static hosting, see README.md).

Also builds:
  - /dist/index.html        a homepage listing every published article
  - /dist/<niche>/index.html  a per-niche listing page
  - /dist/<niche>/<slug>/index.html  each article

No build framework needed -- this is a ~100-line script on purpose, so it's
easy to read and change later.
"""
import datetime
import pathlib
import shutil

import markdown
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLISHED_DIR = ROOT / "content" / "published"
TEMPLATE_PATH = ROOT / "site" / "template.html"
DIST_DIR = ROOT / "dist"

SITE_NAME = "BH Base Media"  # TODO: swap for the real site name/domain


def render(template: str, **kwargs) -> str:
    out = template
    for key, val in kwargs.items():
        out = out.replace("{{" + key + "}}", val)
    return out


def load_articles():
    articles = []
    for md_path in PUBLISHED_DIR.glob("*/*.md"):
        raw = md_path.read_text()
        if raw.startswith("---"):
            _, fm_text, body = raw.split("---", 2)
            fm = yaml.safe_load(fm_text)
        else:
            fm, body = {}, raw
        fm["_niche_dir"] = md_path.parent.name
        fm["_body"] = body.strip()
        articles.append(fm)
    return sorted(articles, key=lambda a: a.get("title", ""))


def main():
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)

    template = TEMPLATE_PATH.read_text()
    articles = load_articles()
    year = str(datetime.date.today().year)
    nav = '<a href="/">Home</a>'

    # per-article pages
    cards_html = []
    for a in articles:
        niche, slug = a["_niche_dir"], a.get("slug", "untitled")
        out_dir = DIST_DIR / niche / slug
        out_dir.mkdir(parents=True, exist_ok=True)

        body_html = markdown.markdown(a["_body"], extensions=["extra"])
        disclosure_html = ""  # disclosures are written inline by the writer agent,
                               # already inside the markdown body per seo-article rules

        page = render(
            template,
            TITLE=a.get("title", "Untitled"),
            DESCRIPTION=a.get("title", ""),
            SITE_NAME=SITE_NAME,
            NAV=nav,
            YEAR=year,
            CONTENT=(
                f'<p class="eyebrow">{niche.replace("-", " ")}</p>'
                f'<h1>{a.get("title", "Untitled")}</h1>'
                f'<article>{body_html}</article>'
            ),
        )
        (out_dir / "index.html").write_text(page)

        cards_html.append(
            f'<a class="card" href="/{niche}/{slug}/">'
            f'<p class="eyebrow">{niche.replace("-", " ")}</p>'
            f'<h3>{a.get("title", "Untitled")}</h3></a>'
        )

    # homepage
    homepage = render(
        template,
        TITLE=SITE_NAME,
        DESCRIPTION=f"{SITE_NAME} -- articles",
        SITE_NAME=SITE_NAME,
        NAV=nav,
        YEAR=year,
        CONTENT=f'<div class="card-grid">{"".join(cards_html)}</div>' if cards_html
                else "<p>Nothing published yet.</p>",
    )
    (DIST_DIR / "index.html").write_text(homepage)

    print(f"[ok] built {len(articles)} article(s) -> {DIST_DIR}")


if __name__ == "__main__":
    main()
