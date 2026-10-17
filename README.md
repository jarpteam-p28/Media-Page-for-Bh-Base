# BH Base Media Pipeline

An automated research → draft → human-approval → publish → cross-post pipeline
for a BoxLife/Daily-Mail-style media site, run entirely on free infrastructure
(GitHub Actions + Cloudflare Pages).

**Nothing publishes without a human clicking merge.** That's by design, not a
limitation -- see "How approval works" below.

## How it works, end to end

```
research_agent.py      -->  finds trending stories per niche (Google News RSS + Reddit)
        |
writer_agent.py         -->  drafts a full article per niche, flags anything unsure of
        |
review_check.py          -->  automated checklist: dead links, missing disclosures,
        |                      AI-writing tells, unverified claims
        |
   [ GitHub PR opened ]   -->  THIS is where a human reads and decides
        |
   you edit/merge/close
        |
promote-on-merge.yml      -->  merge moves the file: content/drafts/ -> content/published/
        |
build.py                  -->  rebuilds the static site from everything in content/published/
        |
Cloudflare Pages           -->  auto-deploys on every push to main (free)
        |
crosspost_facebook.py       -->  (manual for now) posts the link to your Facebook Page
```

## How approval works

1. Every morning (or whenever you trigger it manually), a GitHub Action runs
   the research + writer agents and opens **one pull request per new draft**.
2. You read the PR. You can edit the file right there in GitHub's PR view if
   something's off, leave comments, or just read it.
3. **Merge it → it goes live** (the site rebuilds and Cloudflare deploys
   automatically). **Close it without merging → nothing happens**, the draft
   just sits there.

There is no code path from "written" to "live" that skips a merge.

## One-time setup you need to do (I can't do these from code)

1. **Anthropic API key** -- for the writer agent to actually draft articles.
   Add it as a repo secret: Settings → Secrets and variables → Actions →
   `ANTHROPIC_API_KEY`.
2. **Connect Cloudflare Pages (free) to this repo** -- pages.cloudflare.com →
   Create a project → connect to GitHub → pick this repo → build command
   `python3 site/build.py` → output directory `dist`. Every push to `main`
   then deploys automatically. (Netlify or GitHub Pages work the same way if
   you'd rather use one of those.)
3. **Facebook cross-posting** (optional, can wait) -- see the setup checklist
   at the top of `scripts/crosspost_facebook.py`. This one has real friction:
   it's Meta's app-review process, not something code can route around.
4. **GitHub Actions needs to be allowed to push/open PRs** -- in this repo's
   Settings → Actions → General → Workflow permissions, pick "Read and write
   permissions" and check "Allow GitHub Actions to create and approve pull
   requests."

## Adding a new niche

Everything is driven by `config/niches.yaml` -- add a new block there (slug,
voice, audience, search terms, subreddits, required disclosures) and the
research + writer agents pick it up automatically on the next run. No code
changes needed for a new topic, only a new voice (see `VOICE_NOTES` in
`agents/writer_agent.py`) if it's not Rosanna or the plain narrator. To have
Rosanna write a topic, set `voice: rosanna` on it and add her disclosure line
("Rosanna is an AI-created persona...") to that topic's `disclosures`.

## Running it locally (to test before trusting the schedule)

```bash
pip install -r requirements.txt
python3 agents/research_agent.py
ANTHROPIC_API_KEY=sk-... python3 agents/writer_agent.py
python3 agents/review_check.py --all
python3 site/build.py && open dist/index.html   # preview the built site
```

## Why it's built this way

- **RSS/Reddit for trend discovery, not a paid trends API** -- free, no key,
  good enough signal for "what's being talked about right now" per niche.
- **PR-as-approval-queue** -- GitHub already has a review UI, a diff view, and
  a merge button. Building a custom approval dashboard would just be a worse
  version of something free that already exists.
- **Flat markdown + a ~100-line build script, no site framework** -- easy to
  read, easy to change later, nothing to upgrade or break.
- **The writer agent's system prompt bakes in rules from the house content
  skills** (seo-article structure, claim-sourcing citation discipline,
  human-voice/plain-language-gate tone, per-niche writing_rules for health, money and other sensitive topics) so
  drafts already follow them before a human ever looks at them -- review is
  about catching what slipped through, not teaching the model the rules from
  scratch every time.
