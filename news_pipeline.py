#!/usr/bin/env python3
"""
PhiNews Pipeline: RSS → Claude Rewrite → WordPress Draft + Slack Notify
Simple version - no fact boxes, just plain text.
"""

import os
import json
import feedparser
import requests
from datetime import datetime
from anthropic import Anthropic
from bs4 import BeautifulSoup

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
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Parse MAX_ARTICLES_OVERRIDE
try:
    MAX_ARTICLES_OVERRIDE = int(os.getenv("MAX_ARTICLES_OVERRIDE", "1").strip())
except ValueError:
    MAX_ARTICLES_OVERRIDE = 1

print(f"Running news pipeline. Max articles per run: {MAX_ARTICLES_OVERRIDE}")

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

# ===== Helper: Post to WordPress =====
def post_to_wordpress(headline, article_text, source_url, source_name):
    """Post plain text article to WordPress as draft."""
    try:
        wp_base = WP_URL.rstrip("/")
        posts_url = f"{wp_base}/wp-json/wp/v2/posts"
        auth = (WP_USERNAME, WP_APP_PASSWORD)
        
        # Simple HTML: article text + source link
        content_html = f"<p>{article_text.replace(chr(10), '</p><p>')}</p>\n<p><strong>Source:</strong> <a href=\"{source_url}\" target=\"_blank\">{source_name}</a></p>"
        
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
        color = "good" if status == "posted" else "warning"
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
            
            # Skip video URLs
            if "/video/" in url:
                print(f"Skipping video URL: {url}")
                seen.append(url)
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
            system_prompt = """You are a news editor. Rewrite the article in inverted-pyramid style:
- Lead (1 sentence, max 15 words): most important fact
- Body (2-3 paragraphs): context and key details
- Conclusion (1 sentence): impact summary

Rules:
- Max 150 words total
- No sentence over 15 words
- Plain text only, no markdown or formatting
- Precise, concise, no filler"""
            
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
            
            # Extract headline from first sentence
            headline = written.split("\n")[0].strip()
            if len(headline) > 100:
                headline = headline[:97] + "..."
            
            # Title case
            headline = headline.title()
            
            # Check for duplicates in WordPress
            if draft_exists_in_wordpress(headline, url):
                print(f"  Draft already exists in WordPress.")
                skipped += 1
                seen.append(url)
                continue
            
            # Post to WordPress
            post_id, post_link = post_to_wordpress(headline, written, url, source_name)
            if post_id:
                print(f"  Posted to WordPress as draft (ID {post_id})")
                notify_slack(headline, "posted", f"Source: {source_name}")
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
