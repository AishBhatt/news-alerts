#!/usr/bin/env python3
"""
Polls RSS feeds and posts new headlines to Slack via webhook.
Tracks seen items in seen.json (committed back to the repo) to avoid duplicate posts.
"""

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
SEEN_FILE = Path("seen.json")
MAX_SEEN_PER_FEED = 200  # cap memory so the file doesn't grow forever

FEEDS = {
    "World": [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("Reuters World", "https://www.reutersagency.com/feed/?best-topics=world&post_type=best"),
    ],
    "Tech": [
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("The Verge", "https://www.theverge.com/rss/index.xml"),
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("Wired", "https://www.wired.com/feed/rss"),
    ],
    "Business": [
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("Reuters Business", "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best"),
        ("Financial Times", "https://www.ft.com/rss/home"),
    ],
    "Entertainment": [
        ("Variety", "https://variety.com/feed/"),
        ("Hollywood Reporter", "https://www.hollywoodreporter.com/feed/"),
        ("Entertainment Weekly", "https://ew.com/feed/"),
    ],
}


def fetch_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-alert-bot)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    items = []
    # Standard RSS 2.0
    for item in root.findall(".//item"):
        title = item.findtext("title", default="").strip()
        link = item.findtext("link", default="").strip()
        guid = item.findtext("guid", default="").strip() or link
        if title and link:
            items.append({"title": title, "link": link, "id": guid})
    return items


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def post_to_slack(category, source, title, link):
    payload = {
        "text": f"*[{category}]* <{link}|{title}>  _({source})_"
    }
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def main():
    if not WEBHOOK_URL:
        print("ERROR: SLACK_WEBHOOK_URL environment variable not set.", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    total_posted = 0

    for category, sources in FEEDS.items():
        for source_name, url in sources:
            key = f"{category}::{source_name}"
            seen_ids = set(seen.get(key, []))

            try:
                items = fetch_feed(url)
            except Exception as e:
                print(f"WARN: failed to fetch {source_name} ({url}): {e}", file=sys.stderr)
                continue

            new_items = [it for it in items if it["id"] not in seen_ids]

            # On first-ever run for a feed, don't spam Slack with the whole backlog —
            # just record everything as seen and move on.
            if key not in seen:
                seen[key] = [it["id"] for it in items][:MAX_SEEN_PER_FEED]
                print(f"INIT: {key} — recorded {len(items)} existing items, no posts.")
                continue

            for it in reversed(new_items):  # oldest first
                try:
                    post_to_slack(category, source_name, it["title"], it["link"])
                    total_posted += 1
                except Exception as e:
                    print(f"WARN: failed to post to Slack: {e}", file=sys.stderr)

            updated_ids = [it["id"] for it in items] + [i for i in seen_ids if i not in {it['id'] for it in items}]
            seen[key] = updated_ids[:MAX_SEEN_PER_FEED]

    save_seen(seen)
    print(f"Done. Posted {total_posted} new item(s).")


if __name__ == "__main__":
    main()
