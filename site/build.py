#!/usr/bin/env python3
"""
Static site builder -- Stage 5 of the pipeline.

Reads every approved article in content/published/*/*.md (markdown + YAML
frontmatter), renders it with site/template.html, and writes the result into
/dist (ready to deploy to Cloudflare Pages, Netlify, or GitHub Pages -- all
free static hosting, see README.md).

Builds:
  - /dist/index.html                 homepage: lead story, top stories, a block
                                     per section, and a "Latest" column
  - /dist/<niche>/index.html         one page per section
  - /dist/<niche>/<slug>/index.html  each article

Sections, their order and names come from config/niches.yaml. Article dates
come from the `date` frontmatter field, or the YYYY-MM-DD at the start of the
filename. Photos come from the `image*` fields that agents/image_agent.py adds.

No build framework needed -- plain Python on purpose, so it's easy to read and
change later.
"""
import datetime
import html
import pathlib
import shutil

import markdown
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLISHED_DIR = ROOT / "content" / "published"
IMAGES_DIR = ROOT / "content" / "images"
NICHES_FILE = ROOT / "config" / "niches.yaml"
TEMPLATE_PATH = ROOT / "site" / "template.html"
DIST_DIR = ROOT / "dist"
LEGAL_DIR = ROOT / "content" / "legal"  # privacy, terms, disclaimer (markdown)
STATIC_DIR = ROOT / "site" / "static"  # logo, mascot and icons, copied as-is to /dist/static

SITE_NAME = "The Savvy Rabbit"
TAGLINE = "Smart Reads. Better Living."
PUBLISHER = "BH Base LLC"

# One color per section, used for its tab, labels and photo placeholders.
SECTION_COLORS = {
    "health-holistic-trends": "#1e8a5a",
    "viral-human-interest": "#e2561b",
    "money-everyday-life": "#1565c0",
    "dating-relationships": "#c2185b",
}
FALLBACK_COLORS = ["#6a1b9a", "#00838f", "#5d4037", "#455a64"]

# The AI-persona columnists whose short takes appear under an article (the `takes`
# field written by agents/writer_agent.py).
COLUMNISTS = {
    "rosanna": {"name": "Rosanna", "tagline": "The friend who tells you the truth", "color": "#c2185b"},
    "destiny": {"name": "Destiny", "tagline": "The one who already knew", "color": "#6a1b9a"},
    "expert": {"name": "The Expert", "tagline": "Shows the reasoning, names the research", "color": "#1565c0"},
}

e = html.escape  # every value from frontmatter goes through this before reaching HTML


def load_sections():
    niches = yaml.safe_load(NICHES_FILE.read_text())["niches"]
    sections = {}
    for i, n in enumerate(niches):
        color = SECTION_COLORS.get(n["slug"], FALLBACK_COLORS[i % len(FALLBACK_COLORS)])
        sections[n["slug"]] = {"slug": n["slug"], "name": n["name"], "color": color}
    return sections


def parse_date(fm: dict, filename: str) -> datetime.date:
    raw = str(fm.get("date") or filename[:10])
    try:
        return datetime.date.fromisoformat(raw[:10])
    except ValueError:
        return datetime.date.min


def load_articles(sections: dict):
    articles = []
    for md_path in PUBLISHED_DIR.glob("*/*.md"):
        raw = md_path.read_text()
        if raw.startswith("---"):
            _, fm_text, body = raw.split("---", 2)
            fm = yaml.safe_load(fm_text) or {}
        else:
            fm, body = {}, raw
        niche = md_path.parent.name
        sections.setdefault(niche, {"slug": niche, "name": niche.replace("-", " ").title(),
                                    "color": FALLBACK_COLORS[0]})
        fm["_niche"] = niche
        fm["_body"] = body.strip()
        fm["_date"] = parse_date(fm, md_path.name)
        fm.setdefault("slug", md_path.stem)
        fm.setdefault("title", "Untitled")
        articles.append(fm)
    return sorted(articles, key=lambda a: (a["_date"], a["title"]), reverse=True)


def url_of(a: dict) -> str:
    return f"/{a['_niche']}/{a['slug']}/"


def nice_date(d: datetime.date) -> str:
    return "" if d == datetime.date.min else d.strftime("%B %-d, %Y")


def label(section: dict) -> str:
    return (f'<a class="label" style="color:{section["color"]}" href="/{section["slug"]}/">'
            f'{e(section["name"])}</a>')


def photo(a: dict, section: dict, cls: str) -> str:
    if a.get("image"):
        return (f'<div class="photo {cls}"><img src="{e(a["image"])}" alt="" loading="lazy"></div>')
    return (f'<div class="photo {cls} placeholder" style="background:{section["color"]}">'
            f'<span>{e(section["name"])}</span></div>')


def card(a: dict, sections: dict, size: str = "small") -> str:
    s = sections[a["_niche"]]
    summary = f'<p class="summary">{e(a["summary"])}</p>' if a.get("summary") and size != "small" else ""
    return (f'<article class="card card-{size}">'
            f'<a href="{url_of(a)}">{photo(a, s, "")}</a>'
            f'<div class="card-text">{label(s)}'
            f'<h3><a href="{url_of(a)}">{e(a["title"])}</a></h3>{summary}'
            f'<p class="date">{nice_date(a["_date"])}</p></div></article>')


def latest_list(articles: list, sections: dict) -> str:
    items = "".join(
        f'<li><span class="dot" style="background:{sections[a["_niche"]]["color"]}"></span>'
        f'<a href="{url_of(a)}">{e(a["title"])}</a></li>'
        for a in articles[:10]
    )
    return f'<aside class="latest"><h2>Latest</h2><ol>{items}</ol></aside>'


def nav_html(sections: dict, active: str = "") -> str:
    tabs = [f'<a href="/" class="{"active" if not active else ""}" style="--c:#0B1D35">Home</a>']
    for s in sections.values():
        cls = "active" if s["slug"] == active else ""
        tabs.append(f'<a href="/{s["slug"]}/" class="{cls}" style="--c:{s["color"]}">{e(s["name"])}</a>')
    tabs.append(f'<a href="/about/" class="{"active" if active == "about" else ""}" style="--c:#b8922a">About</a>')
    return "".join(tabs)


def render(template: str, **kwargs) -> str:
    out = template
    for key, val in kwargs.items():
        out = out.replace("{{" + key + "}}", val)
    return out


LEGAL_LINKS = []  # (slug, title) of the legal pages that were built; filled in by load_legal_pages()


def page(template, sections, *, title, description, content, active="", og_image="/static/sage-badge.png"):
    og = f'<meta property="og:image" content="{e(og_image)}">' if og_image else ""
    return render(
        template,
        TITLE=e(title),
        DESCRIPTION=e(description),
        OG_IMAGE=og,
        SITE_NAME=e(SITE_NAME),
        TAGLINE=e(TAGLINE),
        PUBLISHER=e(PUBLISHER),
        LEGAL_LINKS="".join(f' · <a href="/{s}/">{e(n)}</a>' for s, n in LEGAL_LINKS),
        NAV=nav_html(sections, active),
        YEAR=str(datetime.date.today().year),
        CONTENT=content,
    )


def write(path: pathlib.Path, text: str):
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.html").write_text(text)


def takes_html(a: dict) -> str:
    boxes = []
    for t in a.get("takes") or []:
        c = COLUMNISTS.get(t.get("voice"))
        if not c or not t.get("text"):
            continue
        boxes.append(
            f'<div class="take" style="--c:{c["color"]}">'
            f'<div class="take-head"><span class="avatar">{e(c["name"].removeprefix("The ")[0])}</span>'
            f'<div><strong>{e(c["name"])}</strong> <span class="badge">AI persona</span>'
            f'<p class="tagline">{e(c["tagline"])}</p></div></div>'
            f'<div class="take-text">{markdown.markdown(e(t["text"]))}</div></div>')
    if not boxes:
        return ""
    return ('<section class="takes"><h2>Our columnists weigh in</h2>'
            '<p class="takes-note">Our columnists are AI-created personas. Their takes are opinion, '
            'not professional advice.</p>' + "".join(boxes) + "</section>")


def checked_badge(a: dict) -> str:
    fc = a.get("fact_check") or {}
    if not fc.get("done"):
        return ""
    return ('<p class="checked"><span class="tick">✓</span> <strong>Checked twice.</strong> Two independent '
            'fact-checks against the original sources, then approved by an editor. '
            '<a href="/about/#how-we-check">How we check</a></p>')


def load_legal_pages():
    # A page that still has a "[FILL IN" blank (address, contact email...) is not built,
    # so an unfinished policy never goes live.
    pages = []
    for md_path in sorted(LEGAL_DIR.glob("*.md")):
        _, fm_text, body = md_path.read_text().split("---", 2)
        fm = yaml.safe_load(fm_text) or {}
        if "[FILL IN" in body:
            print(f"[skip] {md_path.name}: still has [FILL IN] blanks, not published")
            continue
        pages.append((fm, body))
        LEGAL_LINKS.append((fm["slug"], fm["title"]))
    return pages


def build_legal(template, sections, fm, body):
    updated = fm.get("updated")
    when = f'<p class="byline">Last updated {nice_date(updated)}</p>' if isinstance(updated, datetime.date) else ""
    content = (f'<div class="about legal"><h1>{e(fm["title"])}</h1>{when}'
               f'<div class="body">{markdown.markdown(body.strip(), extensions=["extra"])}</div></div>')
    write(DIST_DIR / fm["slug"], page(template, sections, title=f'{fm["title"]} | {SITE_NAME}',
                                      description=f'{fm["title"]} for {SITE_NAME}', content=content))


def build_about(template, sections):
    content = f"""
<div class="about">
  <div class="about-hero">
    <img class="about-sage" src="/static/sage-full.jpg" alt="Sage, the Savvy Rabbit mascot, in a navy hoodie with sunglasses pushed up on his head" width="280">
    <div>
      <p class="kicker">About us</p>
      <h1>Smart reads for real life.</h1>
      <p class="lede">{e(SITE_NAME)} explains the health trends, money news, viral stories and dating questions
      everyone is talking about, in plain language, with the sources linked.</p>
      <p>Meet Sage, our mascot. He's the rabbit with one folded ear and his sunglasses pushed up, because he'd
      rather read the fine print than look cool. He doesn't write the stories. He's here to remind us what
      we're for: being the savvy friend who checks before sharing.</p>
    </div>
  </div>
  <h2 id="how-we-check">Every story checked twice</h2>
  <ol class="steps">
    <li><strong>We start with the news.</strong> Each story begins with something people are reading or searching for this week.</li>
    <li><strong>We write it plainly.</strong> What happened, what the evidence says, and what it means for you, with links to the original sources.</li>
    <li><strong>First fact-check.</strong> Every number, date, name and quote is checked against the original source.</li>
    <li><strong>Second, independent fact-check.</strong> A second check re-opens every source without trusting the first one.</li>
    <li><strong>An editor approves it.</strong> Nothing is published until a person at {e(PUBLISHER)} signs off.</li>
  </ol>
  <h2>Our columnists are AI personas</h2>
  <p>Under some stories you'll see short takes from Rosanna, Destiny and The Expert. They are AI-created
  personas, labeled "AI persona" every time. Their takes are opinion, not professional advice.</p>
  <h2>Not professional advice</h2>
  <p>Our articles are for general information only. They are not medical, financial, legal or professional
  advice. Talk to a qualified professional about your own situation.</p>
  <h2>Corrections</h2>
  <p>If we get something wrong, we fix it and say so on the story.</p>
  <p class="publisher">{e(SITE_NAME)} is published by {e(PUBLISHER)}.</p>
</div>"""
    write(DIST_DIR / "about", page(template, sections, title=f"About | {SITE_NAME}",
                                   description=f"How {SITE_NAME} picks, writes and double-checks every story.",
                                   content=content, active="about"))


def build_article(template, sections, articles, a):
    s = sections[a["_niche"]]
    body_html = markdown.markdown(a["_body"], extensions=["extra"])
    credit = ""
    if a.get("image"):
        credit = (f'<figcaption>Photo: <a href="{e(a.get("image_credit_url", a["image"]))}" '
                  f'rel="nofollow noopener" target="_blank">{e(a.get("image_credit", "Unknown"))}</a>'
                  f' · {e(a.get("image_license", ""))}</figcaption>')
    hero = f'<figure class="hero">{photo(a, s, "")}{credit}</figure>'
    standfirst = f'<p class="standfirst">{e(a["summary"])}</p>' if a.get("summary") else ""
    more = [x for x in articles if x["_niche"] == a["_niche"] and x is not a][:4]
    more_html = (f'<section class="more"><h2 style="border-color:{s["color"]}">More in {e(s["name"])}</h2>'
                 f'<div class="grid-4">{"".join(card(x, sections) for x in more)}</div></section>') if more else ""
    content = (
        f'<div class="article-wrap"><div class="article-main">{label(s)}'
        f'<h1>{e(a["title"])}</h1>{standfirst}'
        f'<p class="byline">By {e(SITE_NAME)} Staff · <time datetime="{a["_date"].isoformat()}">'
        f'{nice_date(a["_date"])}</time></p>{checked_badge(a)}'
        f'<div class="share"><a data-share="facebook">Facebook</a><a data-share="x">X</a>'
        f'<a data-share="email">Email</a><button type="button" data-share="copy">Copy link</button></div>'
        f'{hero}<div class="body">{body_html}</div>{takes_html(a)}</div>'
        f'{latest_list(articles, sections)}</div>{more_html}'
    )
    write(DIST_DIR / a["_niche"] / a["slug"],
          page(template, sections, title=f'{a["title"]} | {SITE_NAME}', description=a.get("summary") or a["title"],
               content=content, active=a["_niche"], og_image=a.get("image", "")))


def build_section(template, sections, articles, s):
    items = [a for a in articles if a["_niche"] == s["slug"]]
    if items:
        lead, rest = items[0], items[1:]
        grid = "".join(card(a, sections, "medium") for a in rest)
        body = card(lead, sections, "lead") + (f'<div class="grid-3">{grid}</div>' if grid else "")
    else:
        body = '<p class="empty">No stories here yet. Check back soon.</p>'
    content = (f'<h1 class="section-title" style="border-color:{s["color"]};color:{s["color"]}">'
               f'{e(s["name"])}</h1>{body}')
    write(DIST_DIR / s["slug"], page(template, sections, title=f'{s["name"]} | {SITE_NAME}',
                                     description=f'{s["name"]} news from {SITE_NAME}',
                                     content=content, active=s["slug"]))


def build_home(template, sections, articles):
    if not articles:
        content = '<p class="empty">Nothing published yet.</p>'
    else:
        # The lead slot is the newest story that has a photo (falls back to the newest).
        lead = next((a for a in articles if a.get("image")), articles[0])
        top = [a for a in articles if a is not lead][:2]
        blocks = []
        for s in sections.values():
            items = [a for a in articles if a["_niche"] == s["slug"]][:4]
            if not items:
                continue
            blocks.append(
                f'<section class="block"><h2 style="background:{s["color"]}">'
                f'<a href="/{s["slug"]}/">{e(s["name"])} ›</a></h2>'
                f'<div class="grid-4">{"".join(card(a, sections) for a in items)}</div></section>')
        content = (
            f'<div class="home-top"><div class="home-main">{card(lead, sections, "lead")}'
            f'<div class="grid-2">{"".join(card(a, sections, "medium") for a in top)}</div></div>'
            f'{latest_list(articles, sections)}</div>{"".join(blocks)}'
        )
    write(DIST_DIR, page(template, sections, title=f"{SITE_NAME}: {TAGLINE}",
                         description=f"{SITE_NAME}: health, money, viral and relationship stories, explained clearly and checked twice.",
                         content=content))


def main():
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)

    if IMAGES_DIR.exists():  # photos saved by agents/image_agent.py
        shutil.copytree(IMAGES_DIR, DIST_DIR / "images")
    shutil.copytree(STATIC_DIR, DIST_DIR / "static")

    template = TEMPLATE_PATH.read_text()
    sections = load_sections()
    legal_pages = load_legal_pages()
    articles = load_articles(sections)

    for a in articles:
        build_article(template, sections, articles, a)
    for s in sections.values():
        build_section(template, sections, articles, s)
    build_home(template, sections, articles)
    build_about(template, sections)
    for fm, body in legal_pages:
        build_legal(template, sections, fm, body)

    print(f"[ok] built {len(articles)} article(s) in {len(sections)} section(s) -> {DIST_DIR}")


if __name__ == "__main__":
    main()
