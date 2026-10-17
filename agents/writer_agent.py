#!/usr/bin/env python3
"""
Writer agent -- Stage 2 of the pipeline.

Reads content/drafts/_candidates/<slug>.json (made by research_agent.py),
picks the strongest candidate per niche, and calls the Anthropic API to draft
a full article. The system prompt below distills the house rules from several
skills in the anthropic-skills library so every draft follows them even
though the automated run never "invokes a skill" the way a chat session does:

  - seo-article        structure, answer-first, specific attributable facts
  - claim-sourcing      no number without a traceable source; hedge to match evidence
  - human-voice         sounds like a person, not an AI, in the niche's chosen voice
  - plain-language-gate reads at full scroll speed, no jargon, 20-50 y/o audience
  - health-claim-compliance  per-niche `writing_rules` in config/niches.yaml
                        (e.g. no cure claims in health, no stock picks in money)
  - pre-send-check      a mechanical self-check pass before the draft is saved

The output is a DRAFT, not a publish. It is written to
content/drafts/<slug>/<date>-<slug-of-title>.md with YAML frontmatter and is
meant to be opened as a pull request for a human (Pablo) to read, edit, and
approve by merging -- see .github/workflows/research-and-draft.yml.

Requires env var ANTHROPIC_API_KEY. Model is configurable via ANTHROPIC_MODEL
(defaults to a current Claude model) so this doesn't need code changes when
model names update.
"""
import datetime
import json
import os
import pathlib
import re
import sys

import anthropic
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
NICHES_FILE = ROOT / "config" / "niches.yaml"
CANDIDATES_DIR = ROOT / "content" / "drafts" / "_candidates"
DRAFTS_DIR = ROOT / "content" / "drafts"

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# How hard the model thinks before writing: low | medium | high | xhigh | max.
# "medium" keeps a daily article to a few cents; raise it if drafts feel thin.
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "medium")

VOICE_NOTES = {
    "narrator": (
        "Voice: a faceless, neutral narrator. Plain, confident, zero first-person "
        "anecdotes, no persona backstory. States things plainly and lets the facts carry "
        "the authority."
    ),
}

SYSTEM_PROMPT_TEMPLATE = """You are drafting one article for a media site, in the niche "{niche_name}".
Audience: {audience}

{voice_note}

STRUCTURE (from the seo-article house rules):
- Title: phrase it as the question/angle a reader would actually type or feel.
- First two sentences: answer the core question directly. Do not bury the answer.
- Body: one H2 per sub-question, in the order a reader would naturally ask them.
- One idea per paragraph -- a paragraph should stand alone if quoted out of context.
- Define any term on first use.
- End with ONE next step/offer, never three.

FACTS AND CITATIONS (from the claim-sourcing house rules):
- Every statistic or research claim must name a real, checkable source (author/org, year,
  publication) with a URL if you have one. If you are not certain a specific number is
  real, do NOT state it as a specific number -- say it in words ("many people report...")
  instead of inventing a figure.
- Match claim strength to evidence strength: "one study found" vs "research suggests" vs
  "the evidence indicates" are not interchangeable -- use the weakest phrasing that is
  still accurate for what you actually know.
- If you are not sure a claim is true, mark it inline as [VERIFY: <what to check>] rather
  than guessing. A human reviews every draft before it publishes, so flagging is safe --
  inventing is not.

VOICE AND READABILITY (from human-voice / plain-language-gate house rules):
- Write so a reader aged 20-50 gets it on the first pass, no re-reading required.
- No corporate throat-clearing, no "in today's fast-paced world," no listicle padding.
- Avoid the tells of AI-written text: no "it's important to note," no triplet lists for
  their own sake, no overuse of em-dashes, no hollow transitions.
- Contractions are fine. Short sentences are fine. This should read like a person wrote it
  in one sitting, not like a template was filled in.

EXTRA RULES for this niche:
{writing_rules}

REQUIRED DISCLOSURES for this niche: end the article body (just before Sources) with a
short italic note that contains each line below word-for-word. You may also mention them
naturally earlier where they fit.
{disclosures}

OUTPUT FORMAT: Markdown with YAML frontmatter:
---
title: "<title>"
slug: "<url-safe-slug>"
niche: "{niche_slug}"
voice: "{voice}"
source_story: "<the url of the candidate story this was inspired by>"
status: draft
---
<article body in markdown, with an inline Sources section at the end listing every
source you cited, named with author/org and year, with URLs>
"""


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:80]


def extract_markdown(resp) -> str:
    # The response can include thinking blocks before the text, so join only the
    # text blocks, then drop a ```markdown fence if the model wrapped its output in one
    # (the fence would hide the frontmatter from site/build.py).
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    fenced = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```", text, re.DOTALL)
    return fenced.group(1).strip() if fenced else text


def pick_candidate(candidates: list) -> dict | None:
    # Simple heuristic for now: prefer Reddit items with more upvotes (real audience
    # signal), fall back to the first Google News item. A human can always override by
    # hand-editing content/drafts/_candidates/<slug>.json before running this script.
    reddit_items = [c for c in candidates if c.get("upvotes") is not None]
    if reddit_items:
        return max(reddit_items, key=lambda c: c.get("upvotes", 0))
    return candidates[0] if candidates else None


def run():
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    niches = yaml.safe_load(NICHES_FILE.read_text())["niches"]

    for niche in niches:
        slug = niche["slug"]
        cand_file = CANDIDATES_DIR / f"{slug}.json"
        if not cand_file.exists():
            print(f"[skip] no candidates file for {slug}, run research_agent.py first", file=sys.stderr)
            continue

        candidates = json.loads(cand_file.read_text())["candidates"]
        chosen = pick_candidate(candidates)
        if not chosen:
            print(f"[skip] no candidates for {slug}", file=sys.stderr)
            continue

        voice = niche["voice"]
        disclosures = "\n".join(f"- {d}" for d in niche.get("disclosures") or []) or "- None."
        writing_rules = "\n".join(f"- {r}" for r in niche.get("writing_rules") or []) or "- None."
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            niche_name=niche["name"],
            audience=niche["audience"],
            voice_note=VOICE_NOTES.get(voice, ""),
            writing_rules=writing_rules,
            disclosures=disclosures,
            niche_slug=slug,
            voice=voice,
        )

        user_prompt = (
            f"Source story to draw from:\n"
            f"Title: {chosen['title']}\n"
            f"URL: {chosen['url']}\n"
            f"Snippet: {chosen.get('snippet', '')}\n\n"
            f"Write the article now. Use this story as the trigger/angle, not as your "
            f"only source -- bring in whatever else you know that is genuinely sourceable, "
            f"and flag anything you're unsure of with [VERIFY: ...] instead of guessing."
        )

        # fallbacks="default": if the model declines on policy grounds, the API
        # retries on Anthropic's default fallback model inside the same call.
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": EFFORT},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        if resp.stop_reason == "refusal":
            print(f"[skip] model declined to write {slug} from {chosen['url']}", file=sys.stderr)
            continue
        if resp.stop_reason == "max_tokens":
            print(f"[skip] draft for {slug} was cut off before it finished", file=sys.stderr)
            continue
        draft_md = extract_markdown(resp)

        title_match = re.search(r'title:\s*"([^"]+)"', draft_md)
        title = title_match.group(1) if title_match else chosen["title"]
        date_str = datetime.date.today().isoformat()
        out_dir = DRAFTS_DIR / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date_str}-{slugify(title)}.md"
        out_path.write_text(draft_md)
        print(f"[ok] drafted {slug}: {out_path}")


if __name__ == "__main__":
    run()
