"""
News -> Claude Rewrite -> Grammar Check -> Plagiarism Check -> WordPress Draft.

Full pipeline:
  1. Pull new article URLs from RSS feeds
  2. Extract article text
  3. Claude writes an original article as labeled fact boxes (What Happened,
     Where it Happened, Who is Affected, How it Happened, Why it Happened,
     When it Happened, Key Updates) -- only the boxes it has real info for
  4. LanguageTool checks grammar on Claude's combined output
  5. Copyscape checks Claude's combined output against the live web for plagiarism
  6. If grammar is clean AND plagiarism match % is below threshold -> post WordPress draft
     Otherwise -> skip posting, flag it in Slack with the reason (nothing questionable
     reaches WordPress silently)
  7. Slack notification either way

Env vars required:
  ANTHROPIC_API_KEY      - console.anthropic.com (pay-as-you-go API, NOT claude.ai Pro)
  WP_URL                 - e.g. https://yoursite.com
  WP_USERNAME            - WordPress username
  WP_APP_PASSWORD        - WordPress Application Password
  COPYSCAPE_USERNAME     - Copyscape account username
  COPYSCAPE_API_KEY      - Copyscape Premium API key

Env vars optional:
  SLACK_WEBHOOK_URL       - for notifications
  LANGUAGETOOL_URL        - defaults to the free public LanguageTool API endpoint
  PLAGIARISM_THRESHOLD_PCT - max acceptable matched-word %, default 15
  MAX_GRAMMAR_ISSUES      - max acceptable grammar issues before flagging, default 3
  CHECKS_ENABLED          - "true" (default) or "false". Set to "false" to skip
                             grammar/plagiarism checks entirely and post every
                             Claude draft straight to WordPress as a draft, so
                             you can review writing quality first. COPYSCAPE_*
                             are not required when this is "false".
  MAX_ARTICLES_OVERRIDE   - overrides MAX_ARTICLES_PER_RUN below, e.g. "1" to
                             test with a single article at a time.

State: seen_articles.json tracks processed URLs, committed back to the repo.

Published post format: each labeled fact becomes its own bordered "box"
(a Gutenberg Group block with className "detail-box") on the live site.
Only boxes with real content are included -- there is no fixed set of boxes
every article must have. The Source box is always included, and shows the
source site's name (e.g. "BBC News") hyperlinked to the full article URL.
Matching CSS (.detail-box { border: 1px solid #e5e5e5; border-radius: 8px;
padding: 1rem 1.25rem; margin-bottom: 1rem; }) must already be pasted into
Styles > Additional CSS on the WordPress site for these to render boxed.
"""

import os
import re
import sys
import json
import time
import html
import random
from datetime import datetime
from itertools import zip_longest
from urllib.parse import urlparse
import feedparser
import requests
from bs4 import BeautifulSoup

# newspaper3k is imported lazily inside extract_article() because its
# lxml.html.clean dependency (lxml_html_clean) may be missing, and a
# top-level ImportError would kill the whole pipeline before the
# BeautifulSoup fallback could run.

# ---- Config from environment ----
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
WP_URL = os.environ.get("WP_URL", "").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD")
COPYSCAPE_USERNAME = os.environ.get("COPYSCAPE_USERNAME")
COPYSCAPE_API_KEY = os.environ.get("COPYSCAPE_API_KEY")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
LANGUAGETOOL_URL = os.environ.get("LANGUAGETOOL_URL", "https://api.languagetool.org/v2/check")
PLAGIARISM_THRESHOLD_PCT = float(os.environ.get("PLAGIARISM_THRESHOLD_PCT", "15"))
MAX_GRAMMAR_ISSUES = int(os.environ.get("MAX_GRAMMAR_ISSUES", "3"))
_checks_env_raw = os.environ.get("CHECKS_ENABLED", "true")
CHECKS_ENABLED = _checks_env_raw.strip().lower() != "false"

STATE_FILE = "seen_articles.json"

# feed URL -> WordPress category slug the article is filed under
FEEDS = {
    # --- World news ---
    "https://feeds.bbci.co.uk/news/world/rss.xml": "world",
    "https://www.aljazeera.com/xml/rss/all.xml": "world",
    "https://www.theguardian.com/world/rss": "world",
    # --- India ---
    "https://www.thehindu.com/news/national/feeder/default.rss": "india",
    "https://indianexpress.com/section/india/feed/": "india",
    # --- Business ---
    "https://indianexpress.com/section/business/feed/": "business",
    "https://feeds.bbci.co.uk/news/business/rss.xml": "business",
    # --- Sports ---
    "https://www.theguardian.com/sport/rss": "sports",
    # --- Science ---
    "https://www.theguardian.com/science/rss": "science",
    # --- Lifestyle ---
    "https://indianexpress.com/section/lifestyle/feed/": "lifestyle",
    # --- Tech ---
    "https://techcrunch.com/feed/": "technology",
    "https://www.theverge.com/rss/index.xml": "technology",
    # --- Entertainment ---
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml": "entertainment",
}

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
COPYSCAPE_URL = "https://www.copyscape.com/api/"

MAX_ARTICLES_PER_RUN = int(os.environ.get("MAX_ARTICLES_OVERRIDE", "5"))

# Order the boxes appear in on the published page, and the labels shown.
# "key_updates" is the catch-all for real info that doesn't fit the 5 Ws.
BOX_ORDER = [
    ("what_happened", "What Happened"),
    ("where_it_happened", "Where it Happened"),
    ("who_is_affected", "Who is Affected"),
    ("how_it_happened", "How it Happened"),
    ("why_it_happened", "Why it Happened"),
    ("when_it_happened", "When it Happened"),
    ("key_updates", "Key Updates"),
]


# ---- State helpers ----

def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


# ---- Step 1-2: RSS + extraction ----

def get_new_entries(seen):
    """
    Collect unseen links per feed, shuffle the feed order each run, then
    interleave (1st from each feed, then 2nd from each, ...). With
    MAX_ARTICLES_OVERRIDE=1 this means each run picks the newest unseen
    article from a random feed, instead of always draining the first feed.
    A dead or empty feed just contributes nothing and does not break the run.
    """
    per_feed = []
    for feed_url, category in FEEDS.items():
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"  Feed error ({feed_url}): {e}", file=sys.stderr)
            continue
        links = []
        for entry in parsed.entries:
            link = entry.get("link")
            if link and link not in seen and link not in links:
                links.append((link, category))
        if links:
            per_feed.append(links)

    random.shuffle(per_feed)
    interleaved = []
    for group in zip_longest(*per_feed):
        interleaved.extend(link for link in group if link)
    return interleaved


def extract_with_beautifulsoup(url):
    """Fallback: fetch raw HTML, parse paragraphs with BeautifulSoup."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, timeout=10, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        paragraphs = [
            p.get_text(strip=True)
            for p in soup.find_all("p")
            if p.get_text(strip=True) and len(p.get_text(strip=True)) > 20
        ]
        return " ".join(paragraphs).strip()
    except Exception as e:
        print(f"  BeautifulSoup fallback also failed ({e})")
        return ""


def extract_article(url):
    title = ""
    text = ""
    try:
        from newspaper import Article
        article = Article(url)
        article.download()
        article.parse()
        title = article.title or ""
        text = article.text or ""
    except Exception as e:
        print(f"  newspaper failed ({e}), trying BeautifulSoup fallback...")

    if len(text.strip()) < 200:
        print("  newspaper extraction too short, trying BeautifulSoup fallback...")
        text = extract_with_beautifulsoup(url)

    return {"title": title, "text": text, "url": url}


# ---- Step 3: Claude writing ----

def rewrite_with_claude(article_text, original_title):
    system_prompt = (
        "You are a news writer for PhiNews, producing sharp, at-a-glance news articles for "
        "readers who don't normally read news. Based on the source article text given, write "
        "an ORIGINAL article, in your own words and framing rather than mirroring the source's "
        "structure, sentence order, or phrasing.\n\n"
        "Instead of flowing paragraphs, break the article into short labeled fact boxes. The "
        "possible boxes are: what_happened, where_it_happened, who_is_affected, "
        "how_it_happened, why_it_happened, when_it_happened, key_updates.\n\n"
        "CRITICAL RULE: only include a box if the source article actually gives you a real, "
        "specific fact for it. Do NOT guess, invent, or pad a box with a vague restatement "
        "just to fill it in. If the source doesn't say where something happened, omit "
        "where_it_happened entirely -- do not include it as an empty string, and do not include "
        "it with filler like 'Location not specified.' It is completely normal and expected for "
        "most articles to use only 2-4 of these boxes, not all of them.\n\n"
        "what_happened should almost always be present, since it is the core news event. "
        "key_updates is a catch-all: use it only for a real, important fact that doesn't cleanly "
        "belong in any of the other boxes (e.g. an official's quote, a related development, next "
        "steps). If everything already fits into the other boxes, omit key_updates too.\n\n"
        "FORMATTING RULES — STRICTLY ENFORCED:\n"
        "- If a box has 1 sentence: write it as plain text (no bullets).\n"
        "- If a box has 2+ points/facts: format as a bullet list. Each bullet is ONE sentence, 12 words or fewer.\n"
        "- Bullets use this format: put each bullet on a new line starting with '* ' (asterisk space).\n"
        "- Every sentence (bullet or plain) must be 12 words or fewer. Target 10 words per sentence.\n"
        "- HARD LIMIT: no sentence may exceed 12 words. If a thought needs more words, split it into 2-3 separate short sentences or bullets. One idea per sentence.\n"
        "- Precise and concise. No filler adjectives, no repeated points, no fluff.\n"
        "- Neutral tone, direct, concrete. Write like a wire reporter, not an AI.\n"
        "- Plain text only. No markdown backticks, no HTML tags.\n\n"
        "WORD LIMIT:\n"
        "- Total article word count: maximum 200 words TOTAL across all boxes (including the headline).\n"
        "- This is strict. Prioritize clarity and key facts over completeness.\n\n"
        "HEADLINE RULES — STRICTLY ENFORCED:\n"
        "- Maximum 8 words and 55 characters including spaces. Shorter is better.\n"
        "- Lead with the main subject and the key action. Active voice, present tense.\n"
        "- No colons, no quotation marks, no question marks, no clickbait.\n"
        "- Do not copy the original title. Write a fresh, tighter one.\n"
        "- Example: 'Passenger subdues knife attacker on Flydubai flight' is too long. "
        "'Pilot stabbed on Flydubai flight' is correct.\n\n"
        "Respond ONLY with valid JSON, no markdown fences. Include the key \"headline\" plus "
        "ONLY the box keys you actually have real content for, in this shape (example shows "
        "all keys, but you will normally omit several of them):\n"
        '{"headline": "...", "what_happened": "...", "where_it_happened": "...", '
        '"who_is_affected": "...", "how_it_happened": "...", "why_it_happened": "...", '
        '"when_it_happened": "...", "key_updates": "..."}\n\n'
        "BULLET EXAMPLE:\n"
        "If key_updates has 3 facts, format it like this:\n"
        '"key_updates": "* Ethiopia accused Eritrea and Sudan of backing armed groups.\\n* All countries denied allegations.\\n* Fighting could disrupt Ethiopia\'s main import route."'
    )

    user_content = (
        f"Original title (for reference only): {original_title}\n\n"
        f"Source article text:\n{article_text[:6000]}"
    )

    resp = requests.post(
        CLAUDE_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 700,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    raw_text = data["content"][0]["text"]
    cleaned = re.sub(r"```json|```", "", raw_text).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: at least keep the headline and dump everything into
        # what_happened so nothing is silently lost.
        parsed = {"headline": original_title, "what_happened": cleaned}

    parsed = enforce_sentence_length(parsed)
    return parsed


MAX_SENTENCE_WORDS = 12


def _iter_sentences(written):
    for key, _label in BOX_ORDER:
        val = (written.get(key) or "").strip()
        for line in val.split("\n"):
            line = line.strip().lstrip("*").strip()
            for sentence in re.split(r"(?<=[.!?])\s+", line):
                sentence = sentence.strip()
                if sentence:
                    yield key, sentence


def fix_long_sentences(written):
    """Return the (key -> corrected text) for any sentence over the word cap."""
    fixable = {}
    for key, sentence in _iter_sentences(written):
        if len(sentence.split()) > MAX_SENTENCE_WORDS:
            fixable.setdefault(key, []).append(sentence)
    return fixable


REPAIR_ATTEMPTS = 3


def enforce_sentence_length(written):
    """Send the draft back to Claude to split long sentences, retrying until
    every sentence is under the cap. If repair attempts are exhausted, split
    remaining offenders deterministically so nothing over the cap posts."""
    current = written
    for attempt in range(REPAIR_ATTEMPTS):
        offenders = fix_long_sentences(current)
        if not offenders:
            return current
        print(f"[fix] attempt {attempt + 1}: {sum(len(v) for v in offenders.values())} "
              f"sentence(s) over {MAX_SENTENCE_WORDS} words; requesting shorter rewrite")
        repaired = _repair_with_claude(current)
        if repaired is None:
            break
        current = repaired
    remaining = fix_long_sentences(current)
    if remaining:
        print(f"[fix] repair exhausted; splitting {sum(len(v) for v in remaining.values())} "
              f"sentence(s) deterministically")
        current = _split_long_sentences(current)
    return current


def _repair_with_claude(written):
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1000,
        "system": (
            "You tighten news copy for PhiNews. You are given a JSON object of "
            "labeled fact boxes. Rewrite EVERY sentence longer than 12 words into "
            "1-3 shorter sentences (max 12 words each, target 10), preserving all "
            "facts. If a box then contains 2+ sentences, format that box as bullets: "
            "each bullet on its own line starting with '* '. Keep unchanged boxes "
            "exactly as they are. Respond ONLY with the corrected JSON object, same "
            "keys, no markdown fences."
        ),
        "messages": [{"role": "user", "content": json.dumps(written, ensure_ascii=False)}],
    }
    try:
        resp = requests.post(
            CLAUDE_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"]
        repaired = json.loads(re.sub(r"```json|```", "", raw).strip())
        if isinstance(repaired, dict) and repaired.get("headline"):
            return repaired
    except Exception as e:
        print(f"[fix] rewrite failed: {e}")
    return None


def _split_sentence(sentence):
    """Deterministically break one over-cap sentence into <= MAX_SENTENCE_WORDS
    chunks, preferring clause boundaries (commas, semicolons, dashes)."""
    clauses = re.split(r"(?<=[,;—–])\s+|(?<=\S)\s+[—–]\s+", sentence)
    chunks, current = [], ""
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        candidate = f"{current} {clause}".strip()
        if current and len(candidate.split()) > MAX_SENTENCE_WORDS:
            chunks.append(current.rstrip(",;—–"))
            current = clause
        else:
            current = candidate
    if current:
        chunks.append(current)
    # Any chunk still over the cap has no clause boundaries; hard-wrap on words.
    out = []
    for chunk in chunks:
        words = chunk.split()
        while len(words) > MAX_SENTENCE_WORDS:
            out.append(" ".join(words[:MAX_SENTENCE_WORDS]))
            words = words[MAX_SENTENCE_WORDS:]
        if words:
            out.append(" ".join(words))
    return out


def _split_long_sentences(written):
    """Rebuild each box, replacing over-cap sentences with split chunks.
    A box that ends up with 2+ pieces becomes a '* ' bullet list."""
    result = dict(written)
    for key, _label in BOX_ORDER:
        val = (result.get(key) or "").strip()
        if not val:
            continue
        lines = []
        for line in val.split("\n"):
            line = line.strip()
            is_bullet = line.startswith("*")
            text = line.lstrip("*").strip()
            pieces = []
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                sentence = sentence.strip()
                if not sentence:
                    continue
                if len(sentence.split()) > MAX_SENTENCE_WORDS:
                    pieces.extend(_split_sentence(sentence))
                else:
                    pieces.append(sentence)
            if is_bullet or len(pieces) > 1:
                lines.extend(f"* {p}" for p in pieces)
            else:
                lines.extend(pieces)
        result[key] = "\n".join(lines)
    return result


def title_case(headline):
    """Title-case the headline but keep all-caps acronyms (US, UK, AI, UN...) intact."""
    if not headline:
        return headline
    words = headline.split()
    return " ".join(
        w if (len(w) > 1 and w.isupper()) else w[0].upper() + w[1:]
        for w in words
    )


def combined_text(written):
    """All box text concatenated, for grammar/plagiarism checking and logging.
    Strips bullet formatting (* ) for cleaner text."""
    parts = []
    for key, _label in BOX_ORDER:
        val = (written.get(key) or "").strip()
        if val:
            # Remove bullet formatting for plain text output
            cleaned = val.replace('* ', '').replace('\n', ' ')
            parts.append(cleaned)
    return " ".join(parts)


# ---- Step 4: Grammar check (LanguageTool) ----

def check_grammar(text):
    """
    Returns (issue_count, list_of_issue_summaries).
    Fails OPEN (treats as 0 issues) if the API call itself errors, since a
    down grammar-check service should not block publishing entirely -- it
    just means grammar wasn't verified this run, logged clearly either way.
    """
    try:
        resp = requests.post(
            LANGUAGETOOL_URL,
            data={"text": text, "language": "en-US"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        matches = data.get("matches", [])
        summaries = [m.get("message", "") for m in matches[:10]]
        return len(matches), summaries
    except Exception as e:
        print(f"  Grammar check failed (treated as 0 issues, unverified): {e}", file=sys.stderr)
        return 0, ["GRAMMAR CHECK UNAVAILABLE THIS RUN"]


# ---- Step 5: Plagiarism check (Copyscape) ----

def check_plagiarism(text):
    """
    Returns (match_pct, list_of_matched_urls) using Copyscape's text-check
    endpoint (checks raw text against the live web, no need to publish first).
    Fails CLOSED (treats as 100% match / blocks posting) if the API call
    errors, since an unverified plagiarism status should not silently pass --
    better to flag for manual review than risk posting unchecked content.
    """
    try:
        resp = requests.post(
            COPYSCAPE_URL,
            data={
                "u": COPYSCAPE_USERNAME,
                "k": COPYSCAPE_API_KEY,
                "o": "csearch",
                "t": text,
                "c": "1",  # return count/results
                "f": "json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data.get("error"))

        results = data if isinstance(data, list) else data.get("result", [])
        if not results:
            return 0.0, []

        # Copyscape returns per-match percentage; take the highest match found
        max_pct = 0.0
        urls = []
        for r in results:
            pct = float(r.get("textmatch", 0) or r.get("minwordsmatched", 0) or 0)
            max_pct = max(max_pct, pct)
            if r.get("url"):
                urls.append(r["url"])

        return max_pct, urls

    except Exception as e:
        print(f"  Plagiarism check failed (treated as unverified/blocked): {e}", file=sys.stderr)
        return 100.0, ["PLAGIARISM CHECK UNAVAILABLE THIS RUN - flagged for manual review"]


# ---- Step 6: WordPress ----

# Known sites -> display name. Any site not listed here gets a name worked
# out automatically from its domain (e.g. news.sky.com -> "Sky").
# To set an exact name for another site, add one line here.
SOURCE_NAMES = {
    "bbc.com": "BBC News",
    "bbc.co.uk": "BBC News",
    "aljazeera.com": "Al Jazeera",
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "cnbc.com": "CNBC",
    "variety.com": "Variety",
    "reuters.com": "Reuters",
    "nytimes.com": "The New York Times",
    "theguardian.com": "The Guardian",
    "dw.com": "DW",
    "thehindu.com": "The Hindu",
    "indianexpress.com": "The Indian Express",
    "hollywoodreporter.com": "The Hollywood Reporter",
    "deadline.com": "Deadline",
}


def source_name(url):
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, name in SOURCE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov"):
        base = parts[-3]
    elif len(parts) >= 2:
        base = parts[-2]
    else:
        base = host
    return base.replace("-", " ").title()


def source_link_html(url):
    return (
        f'<a href="{html.escape(url, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">'
        f'{html.escape(source_name(url))}</a>'
    )


def _to_sentences(text):
    """Split plain (non-bullet) box text into individual sentences."""
    sentences = []
    for line in text.strip().split('\n'):
        for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
            sentence = sentence.strip()
            if sentence:
                sentences.append(sentence)
    return sentences


def build_box_block(label, text):
    """
    Build a detail box. Box content is always rendered as a bullet list:
    lines starting with '* ' are taken verbatim; plain-text boxes are
    split into sentences first (even a single sentence gets one bullet).
    """
    lines = text.strip().split('\n')
    has_bullets = any(line.strip().startswith('* ') for line in lines)

    if has_bullets:
        items = [line.strip()[2:] for line in lines if line.strip().startswith('* ')]
    else:
        items = _to_sentences(text)

    content_html = '\n'.join(f'<li>{html.escape(item)}</li>' for item in items)
    content_block = (
        f'<!-- wp:list -->\n<ul>\n{content_html}\n</ul>\n<!-- /wp:list -->'
    )
    
    return (
        '<!-- wp:group {"className":"detail-box","layout":{"type":"constrained"}} -->\n'
        f'<div class="wp-block-group detail-box"><!-- wp:paragraph -->\n'
        f'<p><strong>{html.escape(label)}</strong></p>\n<!-- /wp:paragraph -->\n\n'
        f'{content_block}\n</div>\n<!-- /wp:group -->'
    )


def build_body_html(written, source_url):
    blocks = []
    for key, label in BOX_ORDER:
        text = (written.get(key) or "").strip()
        if not text:
            continue
        blocks.append(build_box_block(label, text))

    # Source box is always included.
    source_block = (
        '<!-- wp:group {"className":"detail-box","layout":{"type":"constrained"}} -->\n'
        '<div class="wp-block-group detail-box"><!-- wp:paragraph -->\n'
        '<p><strong>Source</strong></p>\n<!-- /wp:paragraph -->\n\n'
        f'<!-- wp:paragraph -->\n<p>{source_link_html(source_url)}</p>\n'
        '<!-- /wp:paragraph --></div>\n<!-- /wp:group -->'
    )
    blocks.append(source_block)

    return "\n\n".join(blocks)


_category_id_cache = {}


def get_category_id(slug):
    """Look up a WordPress category ID by slug; cached; None if not found."""
    if slug in _category_id_cache:
        return _category_id_cache[slug]
    try:
        resp = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            params={"slug": slug},
            timeout=15,
        )
        resp.raise_for_status()
        cats = resp.json()
        cat_id = cats[0]["id"] if cats else None
    except Exception as e:
        print(f"  Category lookup failed for '{slug}': {e}")
        cat_id = None
    _category_id_cache[slug] = cat_id
    return cat_id


def _fetch_recent_posts(per_page=50):
    """Recent published posts with fields needed for rail/similarity work."""
    resp = requests.get(
        f"{WP_URL}/wp-json/wp/v2/posts",
        params={
            "per_page": per_page,
            "status": "publish",
            "_fields": "id,link,title,date,content,categories",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _category_name_map(ids):
    """Category ID -> name for a set of IDs."""
    names = {}
    if not ids:
        return names
    try:
        resp = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            params={"include": ",".join(str(i) for i in ids), "per_page": 100},
            timeout=15,
        )
        resp.raise_for_status()
        names = {c["id"]: c["name"] for c in resp.json()}
    except Exception:
        pass
    return names


def _post_plain_text(post):
    """Title + tag-free content text for token similarity."""
    title = post.get("title", {}).get("rendered", "")
    content = re.sub(r"<[^>]+>", " ", post.get("content", {}).get("rendered", ""))
    return f"{title} {content}"


# A post counts as "related" to the new story on the same token-overlap
# rule the dedup uses; dupes are already blocked upstream, so anything
# matching here is a genuine earlier chapter of the story.
def _related_posts(posts, new_tokens, source_url):
    related = []
    for p in posts:
        if source_url in p.get("content", {}).get("rendered", ""):
            continue
        old_tokens = _content_tokens(_post_plain_text(p))
        shared = new_tokens & old_tokens
        containment = len(shared) / len(new_tokens) if new_tokens else 0
        if (len(shared) >= DUP_MIN_SHARED_TOKENS
                and containment >= DUP_MIN_CONTAINMENT):
            related.append((len(shared), p))
    related.sort(key=lambda t: t[0], reverse=True)
    return [p for _s, p in related]


def _rail_card_html(post, cat_names):
    link = html.escape(post.get("link", ""), quote=True)
    title = html.escape(post.get("title", {}).get("rendered", "").strip())
    cats = post.get("categories") or []
    cat = cat_names.get(cats[0], "") if cats else ""
    date = ""
    try:
        date = datetime.fromisoformat(
            post.get("date", "").replace("Z", "+00:00")).strftime("%b %-d, %Y").upper()
    except Exception:
        pass
    meta = " · ".join(x for x in (cat.upper(), date) if x)
    return (
        '<!-- wp:group {"className":"rail-card","layout":{"type":"constrained"}} -->\n'
        '<div class="wp-block-group rail-card">'
        f'<!-- wp:paragraph --><p class="rail-title"><a href="{link}">{title}</a></p><!-- /wp:paragraph -->\n'
        f'<!-- wp:paragraph --><p class="rail-meta">{html.escape(meta)}</p><!-- /wp:paragraph -->'
        '</div>\n<!-- /wp:group -->'
    )


def side_rail_html(headline, body_text, detail_box_count, category, source_url):
    """
    Right rail: related stories first (same-story-token match), topped up
    with latest posts from this category. 2 cards for short stories
    (<5 detail boxes), 3 for longer ones, so the rail never outruns
    the article. Empty string when there is nothing to show.
    """
    try:
        posts = _fetch_recent_posts()
    except Exception as e:
        print(f"  Rail lookup failed, skipping rail: {e}")
        return ""

    n_cards = 3 if detail_box_count >= 5 else 2
    new_tokens = _content_tokens(f"{headline} {body_text}")
    related = _related_posts(posts, new_tokens, source_url)

    cat_id = get_category_id(category) if category else None
    picked, seen_ids = [], set()
    for p in related:
        picked.append(p)
        seen_ids.add(p["id"])
        if len(picked) >= n_cards:
            break
    if len(picked) < n_cards and cat_id:
        for p in posts:
            if p["id"] in seen_ids or cat_id not in (p.get("categories") or []):
                continue
            if source_url in p.get("content", {}).get("rendered", ""):
                continue
            picked.append(p)
            seen_ids.add(p["id"])
            if len(picked) >= n_cards:
                break
    if not picked:
        return ""

    cat_ids = {c for p in picked for c in (p.get("categories") or [])}
    cat_names = _category_name_map(cat_ids)
    heading = "Related" if related else (
        f"More in {html.escape(category.replace('-', ' ').title())}"
        if category else "More stories")

    cards = "\n".join(_rail_card_html(p, cat_names) for p in picked)
    return (
        '<!-- wp:group {"className":"rail","layout":{"type":"constrained"}} -->\n'
        '<div class="wp-block-group rail">'
        f'<!-- wp:paragraph --><p class="rail-heading"><strong>{heading}</strong></p><!-- /wp:paragraph -->\n'
        f'{cards}\n</div>\n<!-- /wp:group -->'
    )


def wrap_with_rail(body_html, rail_html):
    """Two-column page: story left (~68%), rail right (~32%)."""
    if not rail_html:
        return body_html
    return (
        '<!-- wp:columns -->\n<div class="wp-block-columns">'
        '<!-- wp:column {"width":"68%"} -->\n'
        '<div class="wp-block-column" style="flex-basis:68%">'
        f'{body_html}'
        '</div>\n<!-- /wp:column -->\n\n'
        '<!-- wp:column {"width":"32%"} -->\n'
        '<div class="wp-block-column" style="flex-basis:32%">'
        f'{rail_html}'
        '</div>\n<!-- /wp:column --></div>\n<!-- /wp:columns -->'
    )


def post_wordpress_draft(headline, written, source_url, category=None):
    body_html = build_body_html(written, source_url)
    body_text = combined_text(written)
    n_boxes = body_html.count('class="wp-block-group detail-box"')
    rail = side_rail_html(headline, body_text, n_boxes, category, source_url)
    body_html = wrap_with_rail(body_html, rail)

    payload = {
        "title": headline,
        "content": body_html,
        "status": "draft",
        "excerpt": make_excerpt(written),
    }
    if category:
        cat_id = get_category_id(category)
        if cat_id:
            payload["categories"] = [cat_id]
        else:
            print(f"  Category '{category}' not found in WordPress, posting uncategorized.")

    resp = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


_DEDUP_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been",
    "its", "it", "that", "this", "after", "before", "over", "under",
    "new", "says", "said", "say", "will", "would", "could", "has", "have",
}


def _stem(word):
    """Cheap suffix stem: attacker/attacked/attacks -> attack."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _content_tokens(text):
    """Stemmed, stopword-free token set for similarity comparisons."""
    return {
        _stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) > 2 and w not in _DEDUP_STOPWORDS
    }


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Same-story detection: enough shared stemmed tokens between the new
# headline+body and an existing post's title+content.
DUP_MIN_SHARED_TOKENS = 4
DUP_MIN_CONTAINMENT = 0.25


def story_already_covered(headline, body_text, source_url):
    """
    Return a reason string if this story is already on the site, else None.

    Checks recent drafts AND published posts:
    - exact headline match
    - source URL already inside a post
    - headline token similarity (catches AI-reworded headlines)
    - body token similarity (catches the same story rewritten differently)
    """
    try:
        resp = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts",
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            params={"status": "any", "per_page": 50},
            timeout=30,
        )
        resp.raise_for_status()
        posts = resp.json()

        new_tokens = _content_tokens(f"{headline} {body_text}")

        for post in posts:
            old_title = post.get("title", {}).get("rendered", "")
            old_content = re.sub(r"<[^>]+>", " ",
                                 post.get("content", {}).get("rendered", ""))

            if old_title.strip() == headline.strip():
                return "identical headline"
            if source_url in post.get("content", {}).get("rendered", ""):
                return "same source URL"

            old_tokens = _content_tokens(f"{old_title} {old_content}")
            shared = new_tokens & old_tokens
            containment = len(shared) / len(new_tokens) if new_tokens else 0
            if (len(shared) >= DUP_MIN_SHARED_TOKENS
                    and containment >= DUP_MIN_CONTAINMENT):
                return (f"likely same story ({len(shared)} shared terms "
                        f"with '{old_title.strip()}')")

        return None
    except Exception as e:
        print(f"  Warning: couldn't check WordPress posts: {e}", file=sys.stderr)
        # Fail open — if we can't check, don't block posting
        return None


def make_excerpt(written):
    """First usable box line as a plain-text excerpt for homepage cards/RSS."""
    for key, _label in BOX_ORDER:
        val = (written.get(key) or "").strip()
        if not val:
            continue
        for line in val.split("\n"):
            line = line.strip().lstrip("*").strip()
            if line:
                return re.split(r"(?<=[.!?])\s+", line)[0]
    return ""


# ---- Step 7: Slack ----

def notify_slack(message):
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=10)
    except Exception as e:
        print(f"Slack notify failed: {e}", file=sys.stderr)


# ---- Main ----

def main():
    print(f"DEBUG: raw CHECKS_ENABLED env value = {_checks_env_raw!r} -> parsed as CHECKS_ENABLED={CHECKS_ENABLED}")

    if os.environ.get("DRY_RUN_CONFIG_ONLY", "").strip().lower() == "true":
        print("DRY_RUN_CONFIG_ONLY=true -- stopping here before any API calls. Config check only.")
        return

    required = [
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ("WP_URL", WP_URL),
        ("WP_USERNAME", WP_USERNAME),
        ("WP_APP_PASSWORD", WP_APP_PASSWORD),
    ]
    if CHECKS_ENABLED:
        required += [
            ("COPYSCAPE_USERNAME", COPYSCAPE_USERNAME),
            ("COPYSCAPE_API_KEY", COPYSCAPE_API_KEY),
        ]
    missing = [name for name, val in required if not val]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if not CHECKS_ENABLED:
        print("CHECKS_ENABLED=false -- grammar and plagiarism checks are BYPASSED this run.")
        print("Every Claude draft will post straight to WordPress for you to review manually.")

    seen = load_seen()
    new_links = get_new_entries(seen)[:MAX_ARTICLES_PER_RUN]

    if not new_links:
        print("No new articles.")
        return

    posted, skipped = 0, 0

    for url, category in new_links:
        try:
            if "/video/" in url:
                print(f"Skipping video URL: {url}")
                seen.add(url)
                continue
            print(f"Processing: {url}")
            article = extract_article(url)
            if not article["text"] or len(article["text"]) < 200:
                print("  Skipping, too little extracted text.")
                seen.add(url)
                continue
            
            # Check word count: max ~200 words per article (rough estimate: 1 word per 5 chars)
            word_count_estimate = len(article["text"]) / 5
            if word_count_estimate > 3000:  # ~600 words is roughly 3000 chars, be generous on input
                print(f"  Skipping, article too long ({word_count_estimate:.0f} words estimated, truncating to first 3000 chars)")
                article["text"] = article["text"][:3000]

            written = rewrite_with_claude(article["text"], article["title"])
            headline = title_case(written.get("headline", article["title"]))
            body_text = combined_text(written)

            if not body_text.strip():
                print("  Skipping, Claude returned no usable box content.")
                seen.add(url)
                continue

            # Check if this story is already covered (draft or published)
            dup_reason = story_already_covered(headline, body_text, url)
            if dup_reason:
                print(f"  Skipping, story already covered ({dup_reason}): {headline}")
                seen.add(url)
                continue

            if CHECKS_ENABLED:
                issue_count, grammar_notes = check_grammar(body_text)
                match_pct, matched_urls = check_plagiarism(body_text)
                grammar_ok = issue_count <= MAX_GRAMMAR_ISSUES
                plagiarism_ok = match_pct <= PLAGIARISM_THRESHOLD_PCT
            else:
                issue_count, match_pct = 0, 0.0
                grammar_ok, plagiarism_ok = True, True
                grammar_notes = []

            # Log exactly which boxes Claude filled in, and what they say.
            box_log_lines = []
            for key, label in BOX_ORDER:
                val = (written.get(key) or "").strip()
                if val:
                    box_log_lines.append(f"    [{label}] {val}")
            print(
                f"  --- DRAFT ---\n  Headline: {headline}\n"
                + "\n".join(box_log_lines)
                + "\n  --- END DRAFT ---"
            )

            if grammar_ok and plagiarism_ok:
                wp_post = post_wordpress_draft(headline, written, url, category)
                edit_link = f"{WP_URL}/wp-admin/post.php?post={wp_post['id']}&action=edit"
                checks_note = "(checks bypassed)" if not CHECKS_ENABLED else f"Grammar issues: {issue_count} | Plagiarism match: {match_pct:.1f}%"
                notify_slack(
                    f"📝 New draft ready: *{headline}*\n"
                    f"{checks_note}\n"
                    f"{edit_link}"
                )
                posted += 1
                print(f"  Posted as draft (grammar: {issue_count} issues, plagiarism: {match_pct:.1f}%)")
            else:
                reasons = []
                if not grammar_ok:
                    grammar_detail = "; ".join(grammar_notes[:3]) if grammar_notes else "Unknown grammar issues"
                    reasons.append(f"{issue_count} grammar issues (max {MAX_GRAMMAR_ISSUES}): {grammar_detail}")
                    print(f"  Grammar issues: {grammar_detail}", file=sys.stderr)
                if not plagiarism_ok:
                    reasons.append(f"{match_pct:.1f}% plagiarism match (max {PLAGIARISM_THRESHOLD_PCT}%)")
                notify_slack(
                    f"⚠️ Draft SKIPPED (not posted): *{headline}*\n"
                    f"Reason: {'; '.join(reasons)}\n"
                    f"Source: {url}"
                )
                skipped += 1
                print(f"  Skipped: {'; '.join(reasons)}")

            seen.add(url)
            time.sleep(1)

        except Exception as e:
            print(f"  Error processing {url}: {e}", file=sys.stderr)
            notify_slack(f"❌ Error processing article, skipped: {url}\n{e}")
            seen.add(url)

    save_seen(seen)
    print(f"Done. Posted {posted}, skipped {skipped}.")


if __name__ == "__main__":
    main()
