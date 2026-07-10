"""Link context fetching + attachment (Phase 3.6.2).

Mirrors herald_media.py's shape: fetch on demand, cache by URL, attach
to candidate posts at digest time. But unlike image context there is NO
LLM in the middle — straight HTTP GET → stdlib HTMLParser → strip
nav/footer/scripts/ads/OG tags → store raw text → ship to the stage-2
LLM as a LINK CONTEXT block. James's call (CHANGELOG 2026-07-05):
"AI summary of an AI summary" loses grounding. We want the actual article
body, not a paraphrase.

Architecture mirrors herald_context.py / herald_media.py:

  enrich_link_batch(posts, max_fetches)
    -> walks every post's outbound link list, canonicalises URLs, fetches
       article bodies via urllib (HTMLParser-extracted), writes rows to
       link_context. Best-effort; failures recorded as 'failed' rows and
       never abort the batch. Returns (attempted, ok, failed, skipped).

  attach_link_context(candidates)
    -> for each candidate, looks up its outbound URLs in link_context,
       mutates the candidate in place:
         candidate["_link_context"] = list[{post_id, url, text}, ...]
         candidate["_link_skipped"] = []  (always set; symmetry with images)

  fetch_link_text(url, ...)
    -> one HTTP fetch + extraction. Returns a tagged dict so callers can
       branch on outcome without try/except. Never raises.

URL handling: bird's --json-full already resolves t.co to expanded_url
upstream (see parser.parse_links). We canonicalise the result by
lowercasing the host and stripping common tracking query keys so
duplicate links collapse to one cache row.

User-Agent: we set a real Mozilla UA + Accept-Language. Many news/blog
sites 403 the default Python UA. Operator can override via
HERALD_LINK_USER_AGENT.
"""
import os
import re as _re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import herald_db


# ---------- env-driven defaults ----------
# (HERALD_LINKS_PER_STORY lives in herald_synthesise.py — that's the consumer.)

HERALD_LINK_MAX_CHARS_RENDER = int(os.environ.get("HERALD_LINK_MAX_CHARS_RENDER", "4000"))
HERALD_LINK_MAX_CHARS_STORE = int(os.environ.get("HERALD_LINK_MAX_CHARS_STORE", "8000"))
HERALD_LINK_MAX_BYTES_FETCH = int(os.environ.get("HERALD_LINK_MAX_BYTES_FETCH", str(2 * 1024 * 1024)))
HERALD_LINK_TIMEOUT = int(os.environ.get("HERALD_LINK_TIMEOUT", "10"))
HERALD_LINK_MIN_TEXT_CHARS = int(os.environ.get("HERALD_LINK_MIN_TEXT_CHARS", "50"))
HERALD_MAX_LINK_FETCHES_PER_CYCLE = int(
    os.environ.get("HERALD_MAX_LINK_FETCHES_PER_CYCLE", "10")
)
HERALD_LINK_USER_AGENT = os.environ.get(
    "HERALD_LINK_USER_AGENT",
    "Mozilla/5.0 Herald/0.1 (+https://herald.jaaaames.com)",
)


# ---------- URL canonicalisation ----------

# Built-in tracking/query-noise keys to drop. Lower-case compare.
_BUILTIN_DROP_KEYS = frozenset({
    "fbclid", "gclid", "ref", "ref_src", "mc_eid", "mc_cid", "igshid",
    "_hsenc", "_hsmi", "mkt_tok", "yclid", "msclkid", "icid",
})

def _extra_drop_keys():
    """Operator-tunable extra keys to strip, comma-separated env."""
    extra = os.environ.get("HERALD_LINK_DROP_QUERY_KEYS", "src")
    return {k.strip().lower() for k in extra.split(",") if k.strip()}

_DROP_KEYS = _BUILTIN_DROP_KEYS | _extra_drop_keys()


def _canonicalise_url(url):
    """Strip tracking params, lowercase scheme + host, preserve path + fragment.

    Pure function. Idempotent: canonicalise(canonicalise(x)) == canonicalise(x).
    Returns the canonical form used as link_context.canonical_url PK.
    """
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url  # unparseable; return as-is, will likely fail at fetch
    scheme = (parts.scheme or "http").lower()
    netloc = parts.netloc.lower()
    # Drop tracking keys from query.
    if parts.query:
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        kept = [(k, v) for (k, v) in pairs
                if k.lower() not in _DROP_KEYS and not k.lower().startswith("utm_")]
        # Stable sort: same canonical order regardless of input param order.
        kept.sort(key=lambda kv: (kv[0], kv[1]))
        new_query = urllib.parse.urlencode(kept)
    else:
        new_query = ""
    return urllib.parse.urlunsplit((scheme, netloc, parts.path, new_query, parts.fragment))


# ---------- low-level HTTP fetch ----------

def _http_get(url, timeout=None):
    """HTTP GET with Herald UA. Returns a tagged dict, never raises.

    Shape: {
      "ok": bool,
      "status_code": int|None,
      "body_bytes": bytes|None,
      "redirect_url": str|None,    # final URL after urllib's default redirect handling
      "error": str|None,
      "error_kind": None|"http"|"timeout"|"url"|"too_large"|"decode"|"other"
    }

    urllib follows up to ~10 redirects by default; we capture the final
    URL via resp.geturl() but don't store it separately (canonical PK is
    the truth for the LLM).
    """
    if timeout is None:
        timeout = HERALD_LINK_TIMEOUT
    req = urllib.request.Request(url, headers={
        "User-Agent": HERALD_LINK_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.1",
        "Accept-Language": "en",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(HERALD_LINK_MAX_BYTES_FETCH + 1)
            if len(body) > HERALD_LINK_MAX_BYTES_FETCH:
                return {
                    "ok": False, "status_code": resp.status,
                    "body_bytes": None, "redirect_url": resp.geturl(),
                    "error": f"body exceeded {HERALD_LINK_MAX_BYTES_FETCH} bytes",
                    "error_kind": "too_large",
                }
            return {
                "ok": True, "status_code": resp.status,
                "body_bytes": body, "redirect_url": resp.geturl(),
                "error": None, "error_kind": None,
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False, "status_code": e.code,
            "body_bytes": None, "redirect_url": None,
            "error": f"HTTP {e.code}", "error_kind": "http",
        }
    except socket.timeout:
        return {
            "ok": False, "status_code": None,
            "body_bytes": None, "redirect_url": None,
            "error": f"timeout after {timeout}s", "error_kind": "timeout",
        }
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        return {
            "ok": False, "status_code": None,
            "body_bytes": None, "redirect_url": None,
            "error": str(reason)[:200], "error_kind": "url",
        }
    except UnicodeDecodeError:
        return {
            "ok": False, "status_code": None,
            "body_bytes": None, "redirect_url": None,
            "error": "non-utf8 body", "error_kind": "decode",
        }
    except Exception as e:
        return {
            "ok": False, "status_code": None,
            "body_bytes": None, "redirect_url": None,
            "error": str(e)[:200], "error_kind": "other",
        }


# ---------- HTML extraction ----------

# Tags whose TEXT CONTENT is suppressed entirely. CSS/JS would produce
# noise; nav/footer/aside/header are chrome not content.
SKIP_TAGS = frozenset({
    "script", "style", "noscript", "nav", "footer", "header", "aside", "form",
})

# Class-name substrings that indicate the element is page chrome we should
# skip even when it lives inside <main>. AP News was the trigger case —
# its <main> wraps everything including the photo carousel (5 captions
# × ~250 chars × 2 layers = ~2500 chars of duplicate carousel text) and
# the lede/title area (redundant with the post text we already have).
# Without this, the 8000-char cap gets saturated by carousel chrome before
# we reach the actual article paragraphs.
SKIP_CLASS_PATTERNS = (
    "Carousel",           # AP News, BBC, others — photo carousels
    "CarouselSlide",      # AP News specific
    "CarouselOverlay",    # AP News overlay captions
    "StoryPage-lede",     # AP News lede/title block (duplicate of post text)
    "Page-lead",          # AP News page lead
    "StoryPage-actions",  # AP News share/save buttons
    "Page-actions",       # AP News share button wrappers (Page-actions-row etc.)
    "ActionBar",          # AP News action bar (share/print)
    "ActionLink",         # AP News individual share buttons
    "Page-translatedStoryLink",  # AP News "read in Spanish" links
    "Promo",              # "more from this section" promo blocks
    "PagePromo",          # AP News specific
    "Author-bio",         # AP News author bio at article end
    "Author-content",     # AP News author block
    "PageListRightRail",  # AP News sidebar "related stories"
    "Sidebar",            # generic sidebar
    "Related",            # generic "related stories"
    "Footer",             # generic footer chrome
    "share-button",       # generic share button
    "social-share",       # generic social share widget
    "Page-addDisclaimer", # AP News disclaimer/legal at end
    "add-disclaimer",     # generic disclaimer
)

# Block-level tags that insert a paragraph break in the extracted text.
BLOCK_TAGS = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "blockquote", "article",
    "div", "section", "tr", "td", "br", "hr",
    "pre",
})

# Semantic landmarks that delimit the page's main content area.
MAIN_LANDMARK_TAGS = frozenset({"main", "article"})

# Class-name substrings that indicate the actual article body container.
# Used because some news sites (e.g. AP News) wrap the entire page in
# <main>, including nav and sidebars — only a div with one of these class
# patterns reliably contains the prose we want.
ARTICLE_BODY_CLASS_PATTERNS = (
    "RichTextStoryBody",   # AP News
    "RichTextBody",        # AP News alt
    "StoryPage-lede",      # AP News lede/intro
    "ArticleBody",         # generic CMS
    "article-body",        # Guardian, Reuters, common
    "articleBody",         # camelCase variant
    "storyBody",           # camelCase variant
    "story-body",          # kebab-case variant
    "post-content",        # blogs (Ghost, Jekyll)
    "postContent",         # camelCase variant
    "entry-content",       # WordPress
    "entryContent",        # camelCase variant
    "ArticleContent",      # generic CMS
)


def _has_main_role(attrs):
    """True if attrs include role='main' (ARIA landmark)."""
    for k, v in attrs:
        if (k or "").lower() == "role" and isinstance(v, str) and v.strip().lower() == "main":
            return True
    return False


def _has_article_body_class(attrs):
    """True if any class attribute matches ARTICLE_BODY_CLASS_PATTERNS.

    Recognises both class="a b" and className="a b" (case-sensitive on the
    pattern; case-insensitive on the attribute name).
    """
    for k, v in attrs:
        if (k or "").lower() in ("class", "classname") and isinstance(v, str):
            for pattern in ARTICLE_BODY_CLASS_PATTERNS:
                if pattern in v:
                    return True
    return False


def _has_skip_class(attrs):
    """True if any class attribute matches SKIP_CLASS_PATTERNS.

    When True, the parser treats the element as chrome (suppresses its
    text content) even if it lives inside a landmark.
    """
    for k, v in attrs:
        if (k or "").lower() in ("class", "classname") and isinstance(v, str):
            for pattern in SKIP_CLASS_PATTERNS:
                if pattern in v:
                    return True
    return False


class MainContentParser(HTMLParser):
    """Extract main article text from a page of HTML.

    Strategy:
      * If the page declares a <main>, <article>, role="main", or a div
        with an article-body class name, only emit text from inside the
        deepest such container. This handles the AP-News-class case
        where <main> wraps the entire page including nav and sidebars,
        and the actual prose lives in a div class="RichTextStoryBody".
      * If no landmark is found at all, fall back to the legacy
        behaviour: emit text everywhere except SKIP_TAGS. This keeps
        simple blog pages (no semantic landmarks, no body-class) working.
      * Suppress SKIP_TAGS entirely regardless of mode.
      * On entry to a BLOCK_TAGS element, emit a paragraph break (only
        inside the active container).
      * On OG/twitter meta tags, suppress any text content (defensive).
      * Collapse all whitespace runs to single spaces.

    Landmark tracking: rather than incrementing/decrementing depth on
    tag open/close (which fails for div-based landmarks because </div>
    doesn't know if its matching <div> had a landmark class), we maintain
    a stack. Push on landmark open, pop on landmark close. Text is
    emitted only when the stack is non-empty.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        # Stack of skip-class divs we've entered and not yet exited.
        # Each entry is the tag name (always "div" for skip-class today)
        # so we can match closes. Cleaner than a depth counter because
        # it can't underflow — pop only if the top matches.
        self._skip_stack = []
        self.main_seen = False
        # Stack of landmark tags we've entered and not yet exited.
        self._landmark_stack = []

    @property
    def in_skip(self):
        return bool(self._skip_stack)

    @property
    def in_main(self):
        """True when text should be emitted.

        In strict mode (main_seen=True): only True when inside a landmark.
        In fallback mode (main_seen=False): always True (provided we're
        not in a SKIP_TAGS subtree).
        """
        if not self.main_seen:
            return True
        return bool(self._landmark_stack)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        # Skip-class check FIRST: photo carousels, ledes, sidebars, etc.
        # If a div has BOTH a skip class (e.g. "Author-bio") AND an article-
        # body class (e.g. "RichTextBody") — as Author-bio does on AP News —
        # we want the skip-class to win so the bio doesn't get mistaken
        # for the article body.
        if tag == "div" and _has_skip_class(attrs):
            self._skip_stack.append(tag)
            return
        # Landmark check: <main>, <article>, role="main", or article-body class.
        if tag in MAIN_LANDMARK_TAGS or _has_main_role(attrs) or _has_article_body_class(attrs):
            was_seen = self.main_seen
            self.main_seen = True
            self._landmark_stack.append(tag)
            # On the FIRST landmark, discard any text captured before we
            # found it. Pre-landmark content is unreliable — could be the
            # page <title>, a meta description, or other <head> chrome —
            # and the 8000-char cap will otherwise get saturated by it
            # before we reach the actual article body.
            if not was_seen:
                self.parts = []
            return
        if tag in SKIP_TAGS:
            self._skip_stack.append(tag)
            return
        if tag == "meta":
            # Suppress OG / twitter meta.
            for k, v in attrs:
                kl = (k or "").lower()
                if kl == "property" and isinstance(v, str) and v.lower().startswith("og:"):
                    return
                if kl == "name" and isinstance(v, str) and v.lower().startswith("twitter:"):
                    return
            return
        if not self.in_main:
            return
        if tag in BLOCK_TAGS and not self.in_skip:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        # Pop the landmark stack if this close matches the innermost open.
        if self._landmark_stack and tag in MAIN_LANDMARK_TAGS:
            self._landmark_stack.pop()
            return
        # For div-class article-body landmarks, pop on </div> if the
        # innermost landmark is a div.
        if tag == "div" and self._landmark_stack and self._landmark_stack[-1] == "div":
            self._landmark_stack.pop()
            return
        # Skip-stack pops: tag-based skip (nav, footer, etc.) pops by
        # exact match; div-based skip pops any </div> when the innermost
        # skip entry is "div".
        if tag in SKIP_TAGS and self._skip_stack and self._skip_stack[-1] == tag:
            self._skip_stack.pop()
            return
        if tag == "div" and self._skip_stack and self._skip_stack[-1] == "div":
            self._skip_stack.pop()
            return

    def handle_data(self, data):
        if self.in_skip:
            return
        if not self.in_main:
            return
        if not data:
            return
        cleaned = _re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        if self.parts and not self.parts[-1].endswith(" ") and not self.parts[-1].endswith("\n"):
            self.parts.append(" ")
        self.parts.append(cleaned)

    def error(self, message):  # HTMLParser calls this on malformed HTML
        pass

    def get_text(self):
        """Join parts, normalise whitespace, truncate to storage cap."""
        text = "".join(self.parts)
        text = _re.sub(r"\n{3,}", "\n\n", text)
        text = _re.sub(r"[ \t]+", " ", text)
        text = text.strip()
        if not text:
            return ""
        if len(text) > HERALD_LINK_MAX_CHARS_STORE:
            truncated = text[:HERALD_LINK_MAX_CHARS_STORE]
            omitted = len(text) - HERALD_LINK_MAX_CHARS_STORE
            return f"{truncated}\n\n…[+{omitted} chars]"
        return text


# ---------- per-URL orchestration ----------

def fetch_link_text(raw_url):
    """Fetch and extract one article body. Returns a tagged dict, never raises."""
    canonical = _canonicalise_url(raw_url)
    if not canonical:
        return {
            "status": "failed", "canonical_url": raw_url or "",
            "text": None, "error": "empty url", "error_kind": "other",
            "bytes": None, "chars": None,
        }
    result = _http_get(canonical)
    if not result["ok"]:
        return {
            "status": "failed", "canonical_url": canonical,
            "text": None, "error": result["error"], "error_kind": result["error_kind"],
            "bytes": None, "chars": None,
        }
    body = result["body_bytes"] or b""
    try:
        html_text = body.decode("utf-8", errors="replace")
    except Exception:
        return {
            "status": "failed", "canonical_url": canonical,
            "text": None, "error": "decode failed", "error_kind": "decode",
            "bytes": len(body), "chars": None,
        }
    parser = MainContentParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as e:
        return {
            "status": "failed", "canonical_url": canonical,
            "text": None, "error": f"parse error: {str(e)[:160]}", "error_kind": "other",
            "bytes": len(body), "chars": None,
        }
    text = parser.get_text()
    if len(text) < HERALD_LINK_MIN_TEXT_CHARS:
        return {
            "status": "failed", "canonical_url": canonical,
            "text": None, "error": "extraction empty", "error_kind": "empty",
            "bytes": len(body), "chars": len(text),
        }
    return {
        "status": "success", "canonical_url": canonical,
        "text": text, "error": None, "error_kind": None,
        "bytes": len(body), "chars": len(text),
    }


# ---------- extractor ----------

def _extract_link_urls(post):
    """Return the outbound link URLs from a post.

    Accepts both keys for the link list:
      - 'links'    — parser output (list[str] of expanded URLs, see parser.py:202-212)
      - 'link_urls' — DB column shape after get_posts_in_window deserialises JSON
                     (herald_db.py:242-247)
    Returns [] for None / missing / non-list values. Order preserved.
    """
    urls = post.get("links")
    if not urls:
        urls = post.get("link_urls")
    if not urls:
        return []
    if isinstance(urls, str):
        return [urls] if urls.strip() else []
    if not isinstance(urls, (list, tuple)):
        return []
    return [u for u in urls if u]


# ---------- batch-level operations ----------

def enrich_link_batch(posts, max_fetches=None):
    """Eagerly fetch article bodies for outbound links in the post batch."""
    if max_fetches is None:
        max_fetches = HERALD_MAX_LINK_FETCHES_PER_CYCLE
    if max_fetches <= 0 or not posts:
        return (0, 0, 0, 0)

    seen = set()
    canonical_urls = []
    for p in posts:
        for raw in _extract_link_urls(p):
            canonical = _canonicalise_url(raw)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            canonical_urls.append(canonical)
    if not canonical_urls:
        return (0, 0, 0, 0)

    conn = herald_db.get_connection()
    cached = herald_db.get_link_texts(conn, canonical_urls)
    to_fetch = [u for u in canonical_urls if u not in cached]
    if not to_fetch:
        conn.close()
        return (0, 0, 0, 0)

    attempted = ok = failed = skipped = 0
    for canonical in to_fetch:
        if attempted >= max_fetches:
            skipped = len(to_fetch) - attempted
            break
        try:
            result = fetch_link_text(canonical)
        except Exception as e:
            herald_db.upsert_link_context(
                conn, canonical, "failed", error=f"unexpected: {str(e)[:160]}",
                content_bytes=None,
            )
            attempted += 1
            failed += 1
            continue
        attempted += 1
        if result["status"] == "success":
            herald_db.upsert_link_context(
                conn, canonical, "success",
                content_text=result["text"],
                content_bytes=result["bytes"],
            )
            ok += 1
        else:
            herald_db.upsert_link_context(
                conn, canonical, "failed",
                error=(result["error"] or "")[:200],
                content_bytes=result["bytes"],
            )
            failed += 1
    conn.commit()
    conn.close()
    return (attempted, ok, failed, skipped)


def attach_link_context(candidates):
    """For each candidate, look up link-context rows and attach."""
    if not candidates:
        return

    all_urls = set()
    for c in candidates:
        for u in _extract_link_urls(c):
            canon = _canonicalise_url(u)
            if canon:
                all_urls.add(canon)
        c["_link_skipped"] = []

    if not all_urls:
        for c in candidates:
            c.setdefault("_link_context", [])
        return

    conn = herald_db.get_connection()
    descriptions = herald_db.get_link_texts(conn, list(all_urls))
    conn.close()

    for c in candidates:
        entries = []
        for u in _extract_link_urls(c):
            canon = _canonicalise_url(u)
            if not canon:
                continue
            row = descriptions.get(canon)
            if not row:
                continue
            if row.get("fetch_status") != "success":
                continue
            text = row.get("content_text") or ""
            if not text:
                continue
            entries.append({
                "post_id": c.get("id", ""),
                "url": canon,
                "text": text,
            })
        c["_link_context"] = entries