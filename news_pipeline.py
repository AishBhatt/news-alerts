"""
News -> Claude Rewrite -> Grammar Check -> Plagiarism Check -> WordPress Draft.

Full pipeline:
  1. Pull new article URLs from RSS feeds
  2. Extract article text
  3. Claude writes an original 100-150 word article
  4. LanguageTool checks grammar on Claude's output
  5. Copyscape checks Claude's output against the live web for plagiarism
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
"""

import os
import re
import sys
import json
import time
import feedparser
import requests
from newspaper import Article

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

FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
]

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
COPYSCAPE_URL = "https://www.copyscape.com/api/"

MAX_ARTICLES_PER_RUN = int(os.environ.get("MAX_ARTICLES_OVERRIDE", "5"))


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
    new_entries = []
    for feed_url in FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"  Feed error ({feed_url}): {e}", file=sys.stderr)
            continue
        for entry in parsed.entries:
            link = entry.get("link")
            if link and link not in seen:
                new_entries.append(link)
    return new_entries


def extract_article(url):
    article = Article(url)
    article.download()
    article.parse()
    return {"title": article.title, "text": article.text, "url": url}


# ---- Step 3: Claude writing ----

def rewrite_with_claude(article_text, original_title):
    system_prompt = (
        "You are a news writer for PhiNews, producing sharp, at-a-glance news articles for "
        "readers who don't normally read news. Based on the source article text given, write "
        "an ORIGINAL 100-150 word news article. Synthesize the facts in your own words and "
        "framing rather than closely mirroring the source's structure, sentence order, or "
        "phrasing.\n\n"
        "Style rules — non-negotiable:\n"
        "- No sentence over 15 words. Aim for 10 words per sentence.\n"
        "- Precise and concise. No filler adjectives, no repeated points padded in just to "
        "hit a word count, no fluff anywhere.\n"
        "- Neutral tone, direct, concrete. Write like an experienced wire reporter, not an AI.\n"
        "- Plain text only. No markdown, no HTML, no tables, no special formatting of any kind.\n\n"
        "Structure — inverted pyramid, 3 paragraphs:\n"
        "1. LEAD: the single most important fact and the main topic, in the first paragraph.\n"
        "2. BODY: other relevant happenings/events around the main news, with concrete "
        "supporting details written as plain prose. If more than 5 entities with their own "
        "data or figures are involved, summarize the key figures in plain sentences rather "
        "than a table.\n"
        "3. CONCLUSION: crisp, not verbose. The impact of the news on the entity involved, "
        "and the impact on the wider domain/sector. Keep this short.\n\n"
        "Respond ONLY with valid JSON, no markdown fences, in this exact shape:\n"
        '{"headline": "...", "body_text": "plain text, no HTML tags, paragraphs separated by newlines"}'
    )
    return system_prompt
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
            "max_tokens": 500,
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
        parsed = {"headline": original_title, "body_text": cleaned}

    return parsed


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

def post_wordpress_draft(headline, body_text, source_url):
    paragraphs = [p.strip() for p in body_text.split("\n") if p.strip()]
    body_html = "".join(f"<p>{p}</p>" for p in paragraphs)
    body_html += f'<p><em>Source: <a href="{source_url}">{source_url}</a></em></p>'

    resp = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        json={"title": headline, "content": body_html, "status": "draft"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


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

    for url in new_links:
        try:
            print(f"Processing: {url}")
            article = extract_article(url)
            if not article["text"] or len(article["text"]) < 200:
                print("  Skipping, too little extracted text.")
                seen.add(url)
                continue

            written = rewrite_with_claude(article["text"], article["title"])
            headline = written.get("headline", article["title"])
            body_text = written.get("body_text", "")

            if not body_text.strip():
                print("  Skipping, Claude returned empty body.")
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

            # Always print the full draft text to the log, so you can read
            # exactly what Claude wrote even when checks are on and it gets skipped.
            print(f"  --- DRAFT TEXT ---\n  Headline: {headline}\n  {body_text}\n  --- END DRAFT ---")

            if grammar_ok and plagiarism_ok:
                wp_post = post_wordpress_draft(headline, body_text, url)
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
                    reasons.append(f"{issue_count} grammar issues (max {MAX_GRAMMAR_ISSUES})")
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
