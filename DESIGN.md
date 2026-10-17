# Herald — Design

## The reader and what "working" means

James reads only the X Following feed (~250 accounts, ~700 posts/day; markets
and Bitcoin, AI/tech builders, US and Australian politics, writers,
shitposters). He reads it by clicking through: quote chains to the bottom,
the post being replied to, images (which often carry the text), links. He
follows people as characters. Previous Herald versions failed him because they
saw only top-level posts and produced either a disjointed summary or an
over-synthesised narrative with no grounding in the feed.

A digest is working when:

1. **The feed shows through.** Most of what's read is real posts, verbatim,
   rendered with their context. Model text is limited to a short opener, setup
   lines where a post can't stand alone, and stories where many accounts react
   to one specific thing.
2. **Context is complete.** A shown quote/reply/image/link post carries what it
   refers to.
3. **It covers what mattered.** The 3–5 things most of the feed was talking
   about, and the best individual posts (jokes, images, odd moments).
4. **Groupings are right.** A story's posts are all about the same specific
   event/claim/post — not just the same topic.
5. **Nothing is invented.** Quotes are verbatim; claims cite posts.
6. **10–15 minutes**, 06:00 and 14:00 Sydney, ≤ ~$0.05 per digest.
7. **James would read it instead of opening X**, and after a week is scrolling
   noticeably less. This is the bar; features are frozen while it's tested.

## Why this shape (2026-10 reset)

The diagnosis (from the August database) found value lost mainly at
*selection*: a 40-post embedding/engagement pre-filter dropped whole
conversations spread across many accounts (on 2026-08-19 none of ~15 posts
about the record BTC short liquidation reached the editor). Collection was
fine but over-polled (every 4 minutes for ~2 new posts per request). Quote
text and reply parents were mostly collected and then dropped; images were not
read at all.

A whole 16-hour window is 40–65k tokens as cards, so one long-context call can
read everything. That removed the pre-filter, embeddings, clustering, the
two-stage stories, eager thread/article fetchers, and the desk/edition
pipelines (~10k → ~1.7k lines).

## Pipeline

- **Collect (every 2h)**: one `bird --quote-depth 3 home -n 200 --following
  --json-full` (spans ~20h, so 2h cadence overlaps comfortably), then up to 25
  `bird read`s for reply parents and quote levels the timeline didn't include,
  most-engaged posts first. Retweets are unwrapped to the original with the
  retweeters recorded; promoted posts (old posting time, unified cards) are
  dropped.
- **Images**: every photo and video thumbnail in the window, its quote chain and
  parents is read once by a vision model into `{kind, text, description,
  point}`. People are described, never identified from their faces.
- **Window cards**: self-threads are folded into one card; identical reposts are
  collapsed; a post outside the window quoted by ≥2 accounts becomes an `r#`
  card shown once. Each card shows engagement, which follows retweeted it, how
  many accounts in the window reacted to it, parent, quote chain, image readings
  and link preview.
- **Plan**: the editor prompt contains the reader description and the
  post/run/story definitions — including the rule that a story needs ≥3
  accounts (or 2 plus the original source) responding to one specific thing,
  and the per-post test for membership. Entries carry priorities.
- **Check**: a separate model gives a belongs/doesn't verdict per story post;
  non-members are removed; a story left with one account dissolves.
- **Write**: stories only, from full cards. Mechanical checks: every paragraph
  cites; citations must be the story's posts; every quoted span (double or
  single quotes) must appear verbatim (case, quote style and sentence
  punctuation ignored) in the cited material — post text, quoted posts, parent,
  image text, link preview. Failures go back for one revision with the problem
  list, then offending sentences are cut.
- **Trim** to `HERALD_READ_MINUTES` (14): drop priority-3 posts, then shorten
  runs, then priority-2 posts. Long posts fold after 90 words, as on X.

## Models (NanoGPT)

Chosen by the eval below (2026-10-17). All are environment-configurable.

| Step | Model | Why |
|---|---|---|
| Vision | `qwen/qwen3.7-flash` | OCR of screenshots/charts as good as Gemini Flash-Lite at ~1/10 the price (~$0.00007/image) |
| Plan | `deepseek/deepseek-v4.1-flash` | Coverage equal to the best tested at ~$0.004 for a 40k-token window |
| Check | `anthropic/claude-haiku-5.5` | DeepSeek-flash misused the schema and removed valid posts; Haiku is reliable, ~$0.002 |
| Write | see CHANGELOG for the current choice | Sonnet 5.5 reads best (~$0.02–0.05/digest); Kimi K2.6 close behind at ~$0.005 |

Rejected: GLM-5.3-flash (ran out of tokens reasoning), Qwen 3.7 Plus and GPT-6
Luna (refused real windows on "safety" grounds), Gemini 3 Flash (fine but 5–10×
the price for similar coverage), full Sonnet 5.5 for planning (~$0.15–0.35 per
digest).

## Eval

`eval/` holds real windows (three August windows rebuilt from the old database;
fresh October windows from saved captures), a human reference per window
(`eval/reference/*.json`: ranked conversations with their posts, must-see
posts, ads), and `score.py`, which reports for each variant: top-5 coverage,
whether those were grouped as stories, mixed stories (posts from different
reference conversations in one story), must-see recall, ads selected, quotes
cut, reading minutes and cost. Run a variant with
`eval/run.py WINDOW TAG --plan MODEL --write MODEL`. Change prompts or models
against these numbers and a read of the output, not vibes.

## Invariants

- Post text is never generated: the UI renders posts from storage.
- Quotes in model prose are verbatim and checked; claims cite posts.
- No feedback buttons, per-account weights, email, push or watchdog (single
  reader; the debug page and run log are the visibility).
- Multi-source is out of scope until X works as a daily habit.
