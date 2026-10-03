#!/usr/bin/env python3
"""
PhiNews Pipeline: RSS → Claude Rewrite → WordPress Draft + Slack Notify

Fetches RSS feeds, extracts article text (with BeautifulSoup fallback), deduplicates,
rewrites with Claude Haiku, checks for duplicates in WordPress drafts, posts as draft,
notifies Slack. Optional grammar + plagiarism checks via LanguageTool + Copyscape.

Environment Variables:
  ANTHROPIC_API_KEY       - Anthropic API key
  WP_URL                  - WordPress site URL (with https://)
  WP_USERNAME             - WordPress username
  WP_APP_PASSWORD         - WordPress app password
  COPYSCAPE_USERNAME      - Copyscape username (if CHECKS_ENABLED=true)
  COPYSCAPE_API_KEY       - Copyscape API key (if CHECKS_ENABLED=true)
  SLACK_WEBHOOK_URL       - Slack webhook URL (optional)
  CHECKS_ENABLED          - 'true'/'false', enable grammar + plagiarism checks
  MAX_ARTICLES_OVERRIDE   - integer, max articles per run (default 1)
"""

import os
import json
import feedparser
import requests
from datetime import datetime
from anthropic import Anthropic
from bs4 import BeautifulSoup
import sys

# Config
RSS_FEEDS = {
    "BBC World": "http://feeds.bbc.co.uk/news/world/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "Guardian World": "https://www.theguardian.com/world/rss",
    "The Hindu National": "https://www.thehindu.com/news/national/?service=rss",
    "Indian Express India": "https://indianexpress.com/section/india/feed/",
    "Indian Express Business": "https://indianexpress.com/section/business/feed/",
    "BBC Business": "http://feeds.bbc.co.uk/news/business/rss.xml",
    "Guardian Sport": "https://www.theguardian.com/sport/rss",
    "Guardian Science": "https://www.theguardian.com/science/rss",
    "Indian Express Lifestyle": "https://indianexpress.com/section/lifestyle/feed/",
    "TechCrunch": "http://feeds.techcrunch.com/TechCrunch/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "BBC Entertainment": "http://feeds.bbc.co.uk/news/entertainment_and_arts/rss.xml",
}

SEEN_FILE = "seen_articles.json"
WP_URL = os.getenv("WP_URL", "").strip()
WP_USERNAME = os.getenv("WP_USERNAME", "").strip()
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "").strip()
COPYSCAPE_USERNAME = os.getenv("COPYSCAPE_USERNAME", "").strip()
COPYSCAPE_API_KEY = os.getenv("COPYSCAPE_API_KEY", "").strip()
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Parse CHECKS_ENABLED
checks_enabled_raw = os.getenv("CHECKS_ENABLED", "false").strip().lower()
CHECKS_ENABLED = checks_enabled_raw in ("true", "1", "yes")
print(f"DEBUG: raw CHECKS_ENABLED env value = '{checks_enabled_raw}' -> parsed as CHECKS_ENABLED={CHECKS_ENABLED}")

# Parse MAX_ARTICLES_OVERRIDE
try:
    MAX_ARTICLES_OVERRIDE = int(os.getenv("MAX_ARTICLES_OVERRIDE", "1").strip())
except ValueError:
    MAX_ARTICLES_OVERRIDE = 1

print(f"CHECKS_ENABLED={CHECKS_ENABLED} -- {'grammar and plagiarism checks are ENABLED.' if CHECKS_ENABLED else 'grammar and plagiarism checks are BYPASSED this run.'}")
print(f"Every Claude draft will post {'after checks' if CHECKS_ENABLED else 'straight'} to WordPress for you to review manually.")

# ===== Helper: Load Seen Articles =====
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return json.load(f)
    return []

# ===== Helper: Save Seen Articles =====
def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)

# ===== Helper: Extract Article Text with BeautifulSoup Fallback =====
def extract_article_text(url):
    """
    Try newspaper3k first. If extraction is <200 chars, fall back to BeautifulSoup.
    Returns extracted text (or empty string if both fail).
    """
    try:
        from newspaper import Article
        article = Article(url, keep_article_body=True)
        article.download()
        article.parse()
        text = article.text.strip()
        
        # If newspaper extracted <200 chars, try BeautifulSoup fallback
        if len(text) < 200:
            print(f"  newspaper extraction too short ({len(text)} chars), trying BeautifulSoup fallback...")
            text = extract_with_beautifulsoup(url)
        
        return text
    except Exception as e:
        print(f"  newspaper failed ({e}), trying BeautifulSoup fallback...")
        return extract_with_beautifulsoup(url)

def extract_with_beautifulsoup(url):
    """
    Fallback: fetch raw HTML, parse with BeautifulSoup, extract all paragraph text.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, timeout=10, headers=headers)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, "html.parser")
        
        # Remove script, style, nav, footer tags
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        
        # Extract paragraphs
        paragraphs = []
        for p in soup.find_all("p"):
            text = p.get_text(strip=True)
            if text and len(text) > 20:  # Skip very short lines
                paragraphs.append(text)
        
        extracted = " ".join(paragraphs).strip()
        return extracted
    except Exception as e:
        print(f"  BeautifulSoup fallback also failed ({e})")
        return ""

# ===== Helper: Draft Exists in WordPress =====
def draft_exists_in_wordpress(headline, source_url):
    """Check if a draft already exists with this headline OR source URL."""
    try:
        wp_base = WP_URL.rstrip("/")
        posts_url = f"{wp_base}/wp-json/wp/v2/posts?status=draft&per_page=100"
        auth = (WP_USERNAME, WP_APP_PASSWORD)
        response = requests.get(posts_url, auth=auth, timeout=10)
        response.raise_for_status()
        drafts = response.json()
        
        for draft in drafts:
            draft_title = draft.get("title", {}).get("rendered", "").strip()
            draft_content = draft.get("content", {}).get("raw", "").strip()
            
            # Match by headline or by source URL in content
            if draft_title == headline or source_url in draft_content:
                return True
        
        return False
    except Exception as e:
        print(f"  Error checking WordPress drafts: {e}")
        return False

# ===== Helper: Build Gutenberg Box Block =====
def build_box_block(label, text):
    """
    Build a Gutenberg 'details' block. If text has bullet points (* prefix),
    render as HTML <ul>. Otherwise plain <p>.
    """
    if not text or not text.strip():
        return ""
    
    text = text.strip()
    
    # Detect bullet points
    lines = text.split("\n")
    has_bullets = any(line.strip().startswith("* ") for line in lines)
    
    if has_bullets:
        # Build <ul>
        items = []
        for line in lines:
            line = line.strip()
            if line.startswith("* "):
                items.append(f"<li>{line[2:]}</li>")
        html_content = f"<ul>{''.join(items)}</ul>"
    else:
        html_content = f"<p>{text}</p>"
    
    return {
        "blockName": "core/details",
        "attrs": {
            "summary": label,
            "open": False
        },
        "innerBlocks": [],
        "innerHtml": html_content
    }

# ===== Helper: Grammar Check =====
def check_grammar(text):
    """Check text with LanguageTool."""
    try:
        response = requests.post(
            "https://api.languagetool.org/v2/check",
            data={"text": text, "language": "en-US"},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        matches = data.get("matches", [])
        return len(matches), matches
    except Exception as e:
        print(f"  Grammar check error: {e}")
        return 0, []

# ===== Helper: Plagiarism Check =====
def check_plagiarism(text):
    """Check text with Copyscape API."""
    try:
        response = requests.post(
            "https://www.copyscape.com/api/",
            data={
                "u": COPYSCAPE_USERNAME,
                "k": COPYSCAPE_API_KEY,
                "o": "csearch",
                "e": "UTF-8",
                "t": text[:500]  # Copyscape has char limit
            },
            timeout=15
        )
        response.raise_for_status()
        
        # Copyscape returns XML; look for <result> tags with percentmatch
        if "insufficient" in response.text.lower():
            return None, "Copyscape: insufficient credits"
        
        matches = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.text)
            for result in root.findall("result"):
                percentmatch = result.get("percentmatch")
                if percentmatch:
                    matches.append(float(percentmatch))
        except:
            pass
        
        max_match = max(matches) if matches else 0
        return max_match, None
    except Exception as e:
        print(f"  Plagiarism check error: {e}")
        return None, str(e)

# ===== Helper: Post to WordPress =====
def post_to_wordpress(headline, blocks, source_url, source_name):
    """Post Gutenberg blocks + Source box to WordPress as draft."""
    try:
        wp_base = WP_URL.rstrip("/")
        posts_url = f"{wp_base}/wp-json/wp/v2/posts"
        auth = (WP_USERNAME, WP_APP_PASSWORD)
        
        # Build Source block
        source_block = {
            "blockName": "core/details",
            "attrs": {
                "summary": "Source",
                "open": False
            },
            "innerBlocks": [],
            "innerHtml": f"<p><a href=\"{source_url}\" target=\"_blank\">{source_name}</a></p>"
        }
        blocks.append(source_block)
        
        # Serialize blocks to plain HTML (simpler, more reliable than Gutenberg syntax)
        content_html = ""
        for block in blocks:
            if block["blockName"] == "core/details":
                summary = block["attrs"].get("summary", "Details")
                inner_html = block.get("innerHtml", "")
                # Wrap in <div> with a heading for the summary
                content_html += f"<h3>{summary}</h3>\n{inner_html}\n"
        
        payload = {
            "title": headline,
            "content": content_html,
            "status": "draft"
        }
        
        response = requests.post(posts_url, json=payload, auth=auth, timeout=10)
        response.raise_for_status()
        post = response.json()
        return post.get("id"), post.get("link")
    except Exception as e:
        print(f"  WordPress post error: {e}")
        return None, None

# ===== Helper: Notify Slack =====
def notify_slack(headline, status, details=""):
    """Send Slack message."""
    if not SLACK_WEBHOOK_URL:
        return
    
    try:
        color = "good" if status == "posted" else "warning" if status == "skipped" else "danger"
        text = f"{status.upper()}: {headline}"
        if details:
            text += f"\n{details}"
        
        payload = {
            "attachments": [{
                "color": color,
                "title": headline,
                "text": details or status.capitalize(),
                "ts": int(datetime.now().timestamp())
            }]
        }
        
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"  Slack notify error: {e}")

# ===== Main =====
def main():
    client = Anthropic()
    seen = load_seen()
    posted = 0
    skipped = 0
    
    for source_name, feed_url in RSS_FEEDS.items():
        feed = feedparser.parse(feed_url)
        
        for entry in feed.entries[:5]:  # Check first 5 per feed
            url = entry.get("link", "").strip()
            if not url or url in seen:
                continue
            
            print(f"Processing: {url}")
            
            # Extract text
            text = extract_article_text(url)
            if len(text) < 200:
                print(f"  Skipping, too little extracted text.")
                skipped += 1
                seen.append(url)
                continue
            
            # Rewrite with Claude
            system_prompt = """You are a news editor. Rewrite articles in inverted-pyramid style:
- Lead: most important fact + topic (1 sentence max, <15 words)
- Body: related happenings/context (use table only if 5+ entities with data)
- Conclusion: crisp impact statement (1 sentence max)

Rules:
- Max 150 words total
- No sentence over 15 words (aim for 10)
- Precise, concise, no filler
- No markdown formatting in body text

If article covers 2+ topics, use fact boxes. For each box, use format:
What Happened: [1-2 sentences]
Where: [location if relevant]
Who: [key entities if relevant]
When: [date/time if relevant]
Why: [context if relevant]
Key Updates: [bullet points with * prefix if 2+ items]

Only include boxes with real content. For bullet points, use:
* First point
* Second point

Output ONLY the article text or fact boxes. No preamble, no markdown formatting."""
            
            user_prompt = f"Article text:\n{text[:3000]}"
            
            try:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}]
                )
                written = response.content[0].text.strip()
            except Exception as e:
                print(f"  Claude error: {e}")
                skipped += 1
                seen.append(url)
                continue
            
            # Extract headline: first sentence that ends with period (or first line if no period)
            lines = written.split("\n")
            headline = ""
            for line in lines:
                line = line.strip()
                if line and not line.startswith("*"):  # Skip bullets
                    headline = line
                    if "." in line:
                        headline = line.split(".")[0] + "."
                    break
            
            if not headline:
                headline = lines[0].strip() if lines else "News Update"
            
            # Title case the headline
            headline = headline.title()
            
            if len(headline) > 100:
                headline = headline[:97] + "..."
            
            # Debug: log what Claude returned
            print(f"  Claude output ({len(written)} chars): {written[:200]}")
            
            # Check for duplicates in WordPress
            if draft_exists_in_wordpress(headline, url):
                print(f"  Draft already exists in WordPress.")
                skipped += 1
                seen.append(url)
                continue
            
            # Grammar + Plagiarism checks
            if CHECKS_ENABLED:
                # Grammar check
                grammar_count, grammar_matches = check_grammar(written)
                grammar_details = ""
                if grammar_matches:
                    grammar_details = "; ".join([
                        f"{m.get('message', 'Issue')} (suggested: {m.get('replacements', [{}])[0].get('value', 'N/A')})"
                        for m in grammar_matches[:3]
                    ])
                
                if grammar_count > 3:  # MAX_GRAMMAR_ISSUES default
                    print(f"  Grammar check failed: {grammar_count} issues found. {grammar_details}")
                    notify_slack(headline, "skipped", f"Grammar issues: {grammar_details}")
                    skipped += 1
                    seen.append(url)
                    continue
                
                # Plagiarism check
                max_match, plagia_error = check_plagiarism(written)
                if plagia_error:
                    print(f"  Plagiarism check error: {plagia_error}")
                    notify_slack(headline, "skipped", f"Plagiarism check failed: {plagia_error}")
                    skipped += 1
                    seen.append(url)
                    continue
                
                if max_match and max_match > 15:  # PLAGIARISM_THRESHOLD_PCT default
                    print(f"  Plagiarism check failed: {max_match}% match found.")
                    notify_slack(headline, "skipped", f"Plagiarism: {max_match}% match")
                    skipped += 1
                    seen.append(url)
                    continue
            
            # Wrap entire Claude output in Article box (simple, reliable)
            blocks = [build_box_block("Article", written)]
            
            # Post to WordPress
            post_id, post_link = post_to_wordpress(headline, blocks, url, source_name)
            if post_id:
                print(f"  Posted to WordPress as draft (ID {post_id})")
                notify_slack(headline, "posted", f"Source: {source_name}\nDraft: {post_link}")
                posted += 1
            else:
                print(f"  Failed to post to WordPress")
                skipped += 1
            
            seen.append(url)
            
            # Check if we've hit the max articles for this run
            if posted >= MAX_ARTICLES_OVERRIDE:
                print(f"Reached MAX_ARTICLES_OVERRIDE ({MAX_ARTICLES_OVERRIDE}), stopping.")
                break
        
        if posted >= MAX_ARTICLES_OVERRIDE:
            break
    
    save_seen(seen)
    print(f"Done. Posted {posted}, skipped {skipped}.")

if __name__ == "__main__":
    main()
