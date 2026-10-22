#!/usr/bin/env python3
"""
Review-check agent -- Stage 3 (automated part of the human-approval step).

Runs against every new draft in content/drafts/**/*.md and writes a short
checklist report as a PR comment (or prints to stdout locally). This does NOT
approve anything -- Pablo is still the one who reads the draft and merges the
PR. This just surfaces the things worth looking at first, so review is fast:

  - Any [VERIFY: ...] tags the writer agent left (claim-sourcing discipline --
    the model flagged something it wasn't sure of instead of inventing it)
  - Source URLs that are dead/unreachable
  - Required niche disclosures missing from the body
  - A quick scan for AI-writing tells (pre-send-check style): stock phrases,
    triplet-itis, overuse of em-dashes
  - Word count, so you know if it's actually a quick read or a slog

Usage: python3 agents/review_check.py content/drafts/womens-confidence-dating/2026-xx-xx-foo.md
       python3 agents/review_check.py --all        (checks every draft)
"""
import pathlib
import re
import sys

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
NICHES_FILE = ROOT / "config" / "niches.yaml"
DRAFTS_DIR = ROOT / "content" / "drafts"

AI_TELLS = [
    r"\bit'?s important to note\b",
    r"\bin today'?s (fast-paced|digital) world\b",
    r"\bat the end of the day\b",
    r"\bin conclusion\b",
    r"\bdelve into\b",
    r"\bunlock(ing)? the\b",
    r"\bgame[- ]chang(er|ing)\b",
]


def load_niche(slug: str) -> dict | None:
    niches = yaml.safe_load(NICHES_FILE.read_text())["niches"]
    return next((n for n in niches if n["slug"] == slug), None)


# Sites that refuse automated checks (login walls, bot blockers, rate limits) answer
# with these codes. That says nothing about whether the link works for a reader.
BLOCKED_CODES = {401, 403, 405, 429, 999}


def check_url(url: str) -> str:
    """Returns "ok", "dead", or "blocked" (the site wouldn't let a robot check it)."""
    try:
        resp = requests.head(url, timeout=10, allow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 400:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    except Exception:
        return "dead"
    if resp.status_code < 400:
        return "ok"
    return "blocked" if resp.status_code in BLOCKED_CODES else "dead"


def check_draft(path: pathlib.Path) -> str:
    text = path.read_text()
    # The frontmatter holds the photo's source link, the columnists' takes, etc. Links,
    # word count and style checks look at the article body only, so the photo link
    # isn't reported as a "source" and the --- lines aren't counted as dashes.
    body = text.split("---", 2)[2] if text.startswith("---") else text
    slug = path.parent.name
    niche = load_niche(slug) or {}

    try:
        shown = path.resolve().relative_to(ROOT)
    except ValueError:  # a draft checked out somewhere else (e.g. by fact-check-existing.yml)
        shown = "/".join(path.parts[-4:])
    report = [f"### Review check: `{shown}`", ""]

    word_count = len(re.findall(r"\w+", body))
    report.append(f"- Word count: **{word_count}**")

    fm = yaml.safe_load(text.split("---", 2)[1]) if text.startswith("---") else {}
    fc = (fm or {}).get("fact_check") or {}
    if fc.get("done"):
        report.append(
            f"- 🔎 **Fact-check (2 independent checks):** {fc.get('confirmed', 0)} confirmed, "
            f"{fc.get('corrected', 0)} corrected, {fc.get('removed', 0)} removed, "
            f"{fc.get('unresolved', 0)} still need you · {fc.get('searches', 0)} searches, "
            f"{fc.get('pages_read', 0)} pages read, ~${fc.get('cost_usd', 0):.2f}")
        for item in fc.get("items", []):
            if item.get("verdict") in ("corrected", "removed", "unresolved"):
                src = f" ([source]({item['source']}))" if item.get("source") else ""
                report.append(f"    - {item['verdict']}: {item.get('note', '')}{src}")
    else:
        report.append("- Fact-check: not run on this draft.")

    verifies = re.findall(r"\[VERIFY:[^\]]*\]", text)
    if verifies:
        report.append(f"- ⚠️ **{len(verifies)} unverified claim(s) flagged by the writer:**")
        for v in verifies:
            report.append(f"    - {v}")
    else:
        report.append("- No [VERIFY:] flags left by the writer.")

    urls = sorted({u.rstrip(".,;:'\"") for u in re.findall(r"https?://[^\s)\]>]+", body)})
    results = {u: check_url(u) for u in urls}
    dead = [u for u, r in results.items() if r == "dead"]
    blocked = [u for u, r in results.items() if r == "blocked"]
    if dead:
        report.append(f"- ⚠️ **{len(dead)} source link(s) did not resolve:**")
        for u in dead:
            report.append(f"    - {u}")
    else:
        report.append(f"- All {len(urls) - len(blocked)} checkable source link(s) resolved.")
    if blocked:
        report.append(f"- ℹ️ {len(blocked)} site(s) block automated checks; open these yourself if in doubt:")
        for u in blocked:
            report.append(f"    - {u}")

    missing_disclosures = [d for d in niche.get("disclosures") or [] if d[:25] not in body]
    if missing_disclosures:
        report.append("- ⚠️ **Required disclosure missing or reworded past recognition:**")
        for d in missing_disclosures:
            report.append(f"    - {d}")
    else:
        report.append("- Required disclosures present.")

    tells = [t for t in AI_TELLS if re.search(t, body, re.IGNORECASE)]
    em_dash_count = body.count("—") + body.count("--")
    if tells or em_dash_count > 6:
        report.append("- ⚠️ **Possible AI-writing tells:**")
        for t in tells:
            report.append(f"    - phrase matching `{t}`")
        if em_dash_count > 6:
            report.append(f"    - em-dash used {em_dash_count} times (worth a skim for overuse)")
    else:
        report.append("- No obvious AI-writing tells detected.")

    return "\n".join(report)


def run():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "--all":
        targets = list(DRAFTS_DIR.glob("*/*.md"))
    else:
        targets = [pathlib.Path(sys.argv[1])]

    for path in targets:
        print(check_draft(path))
        print()


if __name__ == "__main__":
    run()
