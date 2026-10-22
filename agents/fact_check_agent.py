#!/usr/bin/env python3
"""
Fact-check agent -- runs after the writer agent, before a draft reaches Pablo.

Three checks before anything is published:

  Check 1, the researcher: for every [VERIFY: ...] note and every specific fact in
    the article (numbers, dates, names, studies, quotes), searches the web and reads
    the sources, then confirms it (adds the source link), corrects it, or removes /
    softens it if it can't be confirmed. It never fills a gap with a guess.
  Check 2, the auditor: an independent pass that re-opens each source behind
    check 1's changes and confirms the new sentence really says what the source says.
    Anything that doesn't hold up goes back to a [VERIFY: ...] note.
  Check 3, Pablo: the review notes in the draft PR list what was confirmed, corrected,
    removed and still open, each with its source.

Each check runs as two calls: a research call with web search / web fetch (whose
answers carry citations), then a small call without tools that turns the findings
into exact edits as JSON (structured output can't be combined with citations).
Edits are applied by this script, only where the quoted text is found exactly once,
so the model never rewrites the whole article. Columnist takes are opinion and are
not touched.

Results are stored in the draft's frontmatter under `fact_check`, and the draft is
marked so it isn't checked twice. Cost per draft is printed and stored too.

Usage: python3 agents/fact_check_agent.py            (all drafts not yet checked)
       python3 agents/fact_check_agent.py <file.md>  (one or more files)

Requires ANTHROPIC_API_KEY. Web search costs $10 per 1,000 searches plus tokens;
web fetch costs tokens only.
"""
import datetime
import json
import os
import pathlib
import sys

import anthropic
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DRAFTS_DIR = ROOT / "content" / "drafts"

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
EFFORT = os.environ.get("FACT_CHECK_EFFORT", "medium")
MAX_SEARCHES = int(os.environ.get("FACT_CHECK_MAX_SEARCHES", "8"))  # per check, per article
MAX_FETCHES = int(os.environ.get("FACT_CHECK_MAX_FETCHES", "10"))
MAX_CONTINUATIONS = 4
# Prices from https://platform.claude.com/docs/en/about-claude/pricing (Claude Sonnet 5.5).
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_SEARCH = 2.00 / 1e6, 10.00 / 1e6, 0.10 / 1e6, 0.01

RESEARCH_SYSTEM = """You are the fact-checker for a magazine-style news site. Today is {today}.

Check the article below. Work through, in this order:
1. Every [VERIFY: ...] note the writer left.
2. Every other specific factual claim: numbers, percentages, prices, dates, years, named
   people, organisations, studies, papers, laws, quotes, and "according to X" statements.
Skip opinions, advice, and general statements that aren't checkable facts.

For each claim, use web search and web fetch to find and READ a reliable source. Prefer
the original (the study, the government data, the official statement, the outlet that
first reported it) over articles about it. A search snippet alone is not enough to
confirm a number or a quote: open the page.

Then report, for each claim:
- CLAIM: the exact sentence or phrase from the article (copy it character for character,
  including any [VERIFY: ...] note attached to it)
- VERDICT: CONFIRMED, CORRECTED, REMOVE, or UNRESOLVED
- WHAT THE SOURCE SAYS: the relevant fact, in your words
- SOURCE: the URL you actually read (and the publication and date)

Rules:
- Never fill a gap with a guess. If you cannot confirm something after a reasonable search,
  the verdict is REMOVE (if the article works without it) or UNRESOLVED (if a human should
  decide).
- CORRECTED only when a reliable source clearly gives the right fact.
- Link-only sources the article already cites count only if you open them and they say it.
"""

AUDIT_SYSTEM = """You are the second, independent fact-checker for a news site. Today is {today}.
Another checker has just edited the article below. Do not trust their work.

For each change listed, open the source given (web fetch; web search only if the link is
broken) and check that the article's NEW text says exactly what the source supports:
same number, same date, same name, same wording strength ("one study found" vs "research
shows"). Report each change as:
- CHANGE #: the number from the list
- RESULT: OK, or WRONG
- WHY: what the source actually says
- FIX: if WRONG, the corrected text, or "flag" if it should go back to a human
"""

EDITS_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "replacement": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["confirmed", "corrected", "removed", "unresolved"]},
                    "source": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["original", "replacement", "verdict", "source", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["edits"],
    "additionalProperties": False,
}

EDITS_SYSTEM = """Turn a fact-checker's findings into exact edits to an article.

For each claim in the findings, output one edit:
- original: text copied EXACTLY from the article (character for character), long enough to
  appear only once. Include any [VERIFY: ...] note that belongs to the claim.
- replacement: the new text.
  - confirmed: the same text without its [VERIFY: ...] note, with a markdown link to the
    source added where it reads naturally, e.g. "... found in 2023 ([Pew Research](URL))."
  - corrected: the text with the corrected fact and a markdown link to the source.
  - removed: the article with the claim taken out or softened so it no longer states the
    unconfirmed fact; keep the surrounding sentence grammatical. "" deletes it entirely.
  - unresolved: the same text, with its note replaced by [VERIFY: <what a human must check>].
- source: the URL the checker read ("" if none).
- note: one short sentence for the editor on what was checked.
Do not edit anything the findings don't cover. Keep the article's voice."""

AUDIT_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "change": {"type": "integer"},
                    "ok": {"type": "boolean"},
                    "fix": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["change", "ok", "fix", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

AUDIT_FIX_SYSTEM = """Turn an auditor's findings into results JSON, one per change number.
ok: true if the auditor found the change correct. For a WRONG change, fix is the corrected
replacement text if the auditor gave one, otherwise "flag". For OK changes, fix is ""."""


class Usage:
    def __init__(self):
        self.input = self.output = self.cache_read = self.searches = self.fetches = 0

    def add(self, resp):
        u = resp.usage
        self.input += (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.output += u.output_tokens or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu:
            self.searches += getattr(stu, "web_search_requests", 0) or 0
            self.fetches += getattr(stu, "web_fetch_requests", 0) or 0

    def cost(self) -> float:
        return (self.input * PRICE_IN + self.output * PRICE_OUT
                + self.cache_read * PRICE_CACHE_READ + self.searches * PRICE_SEARCH)


def research(client, usage: Usage, system: str, content: str, searches: int, fetches: int) -> str:
    """One tool-using call, resumed on pause_turn; returns the findings text."""
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": searches},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches},
    ]
    messages = [{"role": "user", "content": content}]
    texts = []
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=16000, output_config={"effort": EFFORT},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            system=system, tools=tools, messages=messages,
        )
        usage.add(resp)
        texts += [b.text for b in resp.content if b.type == "text"]
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined")
        if resp.stop_reason != "pause_turn":
            break
        # The server paused its search loop; resend so it resumes where it left off.
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    return "\n".join(texts).strip()


def to_json(client, usage: Usage, system: str, schema: dict, content: str) -> dict:
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=16000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=system, messages=[{"role": "user", "content": content}],
    )
    usage.add(resp)
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"structuring step stopped: {resp.stop_reason}")
    return json.loads("".join(b.text for b in resp.content if b.type == "text"))


def apply_edits(body: str, edits: list):
    applied, skipped = [], []
    for ed in edits:
        orig = ed.get("original", "")
        if orig and body.count(orig) == 1:
            body = body.replace(orig, ed.get("replacement", ""), 1)
            applied.append(ed)
        elif ed.get("verdict") != "confirmed" or "[VERIFY" in orig:
            skipped.append(ed)  # couldn't place it: it stays for the human
    return body, applied, skipped


def check_article(client, path: pathlib.Path) -> dict:
    raw = path.read_text()
    _, fm_text, body = raw.split("---", 2)
    fm = yaml.safe_load(fm_text) or {}
    if fm.get("fact_check", {}).get("done"):
        print(f"[skip] already fact-checked: {path.name}")
        return {}
    today = datetime.date.today().isoformat()
    usage = Usage()
    article = f"# {fm.get('title', '')}\n\n{body.strip()}"

    # Check 1: research, then exact edits.
    findings = research(client, usage, RESEARCH_SYSTEM.format(today=today), article, MAX_SEARCHES, MAX_FETCHES)
    edits = to_json(client, usage, EDITS_SYSTEM, EDITS_SCHEMA,
                    f"ARTICLE:\n{body}\n\nFINDINGS:\n{findings}")["edits"]
    body, applied, skipped = apply_edits(body, edits)

    # Check 2: independent audit of every change that asserts a fact from a source.
    to_audit = [e for e in applied if e["verdict"] in ("confirmed", "corrected") and e.get("source")]
    audited_wrong = 0
    if to_audit:
        changes = "\n".join(f"{i}. NEW TEXT: {e['replacement']}\n   SOURCE: {e['source']}"
                            for i, e in enumerate(to_audit, 1))
        audit = research(client, usage, AUDIT_SYSTEM.format(today=today),
                         f"ARTICLE (after edits):\n{body}\n\nCHANGES TO CHECK:\n{changes}",
                         max(2, MAX_SEARCHES // 4), MAX_FETCHES)
        results = to_json(client, usage, AUDIT_FIX_SYSTEM, AUDIT_FIX_SCHEMA,
                          f"CHANGES:\n{changes}\n\nAUDITOR FINDINGS:\n{audit}")["results"]
        for r in results:
            if r.get("ok") or not 1 <= r.get("change", 0) <= len(to_audit):
                continue
            ed = to_audit[r["change"] - 1]
            if ed["replacement"] and body.count(ed["replacement"]) == 1:
                new = r["fix"] if r.get("fix") and r["fix"] != "flag" else \
                    f"{ed['original'].split('[VERIFY')[0].rstrip()} [VERIFY: second check disagreed: {r.get('why', '')}]"
                body = body.replace(ed["replacement"], new, 1)
                ed["verdict"] = "corrected" if r.get("fix") and r["fix"] != "flag" else "unresolved"
                ed["note"] = f"Second check: {r.get('why', '')}"
                audited_wrong += 1

    items = [{"verdict": e["verdict"], "note": e.get("note", ""), "source": e.get("source", "")}
             for e in applied] + \
            [{"verdict": "unresolved", "note": f"Could not be placed automatically: {e.get('note', '')}",
              "source": e.get("source", "")} for e in skipped]
    counts = {v: sum(1 for i in items if i["verdict"] == v)
              for v in ("confirmed", "corrected", "removed", "unresolved")}
    fm["fact_check"] = {
        "done": True,
        "date": today,
        **counts,
        "second_check_changed": audited_wrong,
        "searches": usage.searches,
        "pages_read": usage.fetches,
        "cost_usd": round(usage.cost(), 3),
        "items": items,
    }
    path.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---" + body)
    print(f"[ok] fact-checked {path.name}: {counts}, second check changed {audited_wrong}, "
          f"{usage.searches} searches, ~${usage.cost():.2f}")
    return fm["fact_check"]


def run():
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    targets = [pathlib.Path(p) for p in sys.argv[1:]] or sorted(DRAFTS_DIR.glob("*/*.md"))
    total = 0.0
    for path in targets:
        try:
            result = check_article(client, path)
            total += result.get("cost_usd", 0) if result else 0
        except Exception as e:  # one bad article shouldn't stop the others or the run
            print(f"[warn] fact-check failed for {path.name}: {e}", file=sys.stderr)
    print(f"[ok] fact-check total: ~${total:.2f}")


if __name__ == "__main__":
    run()
