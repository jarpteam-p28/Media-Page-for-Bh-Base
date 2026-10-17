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
  - ai-persona-limits   an AI persona voice (e.g. Rosanna) never invents a biography,
                        never diagnoses a real person, stays second-person
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

# The four house voices (from the human-voice skill). The narrator writes every main
# article by default; the others appear as "columnists" giving a short take on it
# (see `columnists` in config/niches.yaml). Rosanna, Destiny and the Expert are
# AI-created personas and are labeled that way on the site.
VOICE_NOTES = {
    "narrator": (
        "Voice: a faceless narrator, evidence first, like a good documentary: steady, "
        "specific, slightly understated, sure of the facts and honest about their limits. "
        "No 'I', no persona. Mechanism, not promise. At least one concrete specific (a year, "
        "a number, a named study). Hedges ('often', 'in one study') only where the evidence "
        "is soft. Short declarative sentences, with the occasional longer one that explains."
    ),
    "rosanna": (
        "Voice: Rosanna, the friend who tells you the truth. An AI-created persona, disclosed "
        "as such. Calm, direct; talks to the reader in second person. Short lines, hard stops, "
        "fragments are fine. Says the embarrassing part out loud, understands why you stayed, "
        "never scolds. Light spiritual (timing, purpose), never religious. First person only "
        "for opinion ('Here's what I'd tell you'), never for a past she didn't live. Her "
        "signature reversal ('He's not confused. He's comfortable.') at most once. Never "
        "diagnoses a named real person; unsafe situations go to a professional or hotline."
    ),
    "destiny": (
        "Voice: Destiny, the one who already knew. An AI-created persona, disclosed as such. "
        "Slow, deliberate, knowing; warm with an edge, a little theatrical, never silly. Speaks "
        "about signs and types as if describing someone in the room. Certain in tone but never "
        "promises specific events, money, health or love outcomes. Sensual vocabulary (shadow, "
        "heat, gold, quiet, sharp), never cosmic filler. Ends on a knowing line, not an "
        "instruction."
    ),
    "expert": (
        "Voice: the Expert, a professor who actually explains. An AI-created persona, disclosed "
        "as such. Precise, calm, a little dry, never lecturing. Explains why, not just what. "
        "Names the researcher and year when it matters, and only if certain. Says plainly what "
        "isn't known ('Nobody has tested that directly'). Concrete comparisons, not abstract "
        "ones. Never 'studies show' without saying which."
    ),
}

COLUMNIST_NAMES = {"rosanna": "Rosanna", "destiny": "Destiny", "expert": "The Expert"}

TAKES_SYSTEM_TEMPLATE = """You write the "Our columnists weigh in" section that runs under a
magazine article in the niche "{niche_name}". Each columnist reacts to the article in their
own voice with an opinion: what they'd tell the reader, what the article gets at, what they
would push back on. 60-150 words each. Each take must sound clearly different from the others.

THE COLUMNISTS (write exactly one take for each, in this order):
{voices}

RULES FOR EVERY TAKE:
- These are AI-created personas. No invented biography, memories, clients, or experiences.
- Opinion and perspective only. Do not add new statistics, studies or facts that are not in
  the article. If one is truly needed, mark it [VERIFY: ...].
- Never diagnose or label a real, named person.
- Never promise results, and never give medical, financial or legal instructions beyond what
  the article already says.
- No stock phrases (delve, journey, unlock, empower, game-changer, truly, honestly), no
  colon reveals ("Here's the truth: ..."), no emoji, no lists of three for their own sake.
- End each take on its most specific line, not a summary.

EXTRA RULES for this niche:
{writing_rules}
"""

TAKES_SCHEMA = {
    "type": "object",
    "properties": {
        "takes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "voice": {"type": "string", "enum": list(COLUMNIST_NAMES)},
                    "text": {"type": "string"},
                },
                "required": ["voice", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["takes"],
    "additionalProperties": False,
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
summary: "<one-sentence teaser for the homepage, max 25 words>"
image_query: "<2-4 word stock-photo search for a generic scene that fits the story; no real people's names, no brands>"
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


def write_takes(client, niche: dict, article_md: str, writing_rules: str) -> list:
    columnists = [c for c in niche.get("columnists") or [] if c in COLUMNIST_NAMES]
    if not columnists:
        return []
    voices = "\n".join(f"- {c}: {VOICE_NOTES[c]}" for c in columnists)
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": TAKES_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=TAKES_SYSTEM_TEMPLATE.format(niche_name=niche["name"], voices=voices,
                                            writing_rules=writing_rules),
        messages=[{"role": "user", "content": f"The article:\n\n{article_md}"}],
    )
    if resp.stop_reason in ("refusal", "max_tokens"):
        print(f"[skip] columnist takes for {niche['slug']}: {resp.stop_reason}", file=sys.stderr)
        return []
    text = "".join(b.text for b in resp.content if b.type == "text")
    takes = json.loads(text)["takes"]
    # keep only the columnists this niche asked for, once each, in config order
    by_voice = {t["voice"]: t["text"].strip() for t in takes if t.get("text", "").strip()}
    return [{"voice": c, "text": by_voice[c]} for c in columnists if c in by_voice]


def add_takes(draft_md: str, takes: list) -> str:
    _, fm_text, body = draft_md.split("---", 2)
    fm = yaml.safe_load(fm_text) or {}
    fm["takes"] = takes
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---" + body


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

        voice = niche.get("voice") or "narrator"
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
        if draft_md.startswith("---\n") and not re.search(r"^date:", draft_md, re.MULTILINE):
            draft_md = f'---\ndate: "{date_str}"\n' + draft_md[len("---\n"):]

        # The columnists' takes are a bonus: if they fail, the article still goes out.
        try:
            takes = write_takes(client, niche, draft_md, writing_rules)
            if takes:
                draft_md = add_takes(draft_md, takes)
        except Exception as e:
            print(f"[warn] columnist takes failed for {slug}: {e}", file=sys.stderr)

        out_path.write_text(draft_md)
        print(f"[ok] drafted {slug}: {out_path}")


if __name__ == "__main__":
    run()
