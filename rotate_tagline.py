"""Rotate the site tagline every 6 hours.

Walks TAGLINES in order based on elapsed 6-hour slots since the epoch
(stateless — no stored index), wraps at the end, and writes the current
one into the header template part's .hdr-tag paragraph plus the WP site
description (used by RSS/SEO).
"""
import html
import os
import re
import sys
import time

import requests

WP_URL = os.environ.get("WP_URL", "").rstrip("/")
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")

TAGLINES = [
    "News for people who hate news.",
    "Everything you need. Nothing you don't.",
    "News without the news.",
    "Facts. Full stop.",
    "Less news. More knowing.",
    "News. Brutally edited.",
    "The TL;DR is the story.",
    "The news. TL;DR'd.",
    "Everything you need. Nothing you don't.",
]

SIX_HOURS = 6 * 60 * 60


def tagline_markup(text):
    """Grey lead-in + bold accent on the last sentence."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    if len(sentences) < 2:
        return f"<b>{html.escape(text)}</b>"
    lead = " ".join(sentences[:-1])
    last = sentences[-1]
    return f"{html.escape(lead)} <b>{html.escape(last)}</b>"


def main():
    missing = [n for n, v in (
        ("WP_URL", WP_URL), ("WP_USERNAME", WP_USERNAME),
        ("WP_APP_PASSWORD", WP_APP_PASSWORD)) if not v]
    if missing:
        print(f"Missing env vars: {missing}", file=sys.stderr)
        sys.exit(1)

    auth = (WP_USERNAME, WP_APP_PASSWORD)
    slot = int(time.time() // SIX_HOURS)
    idx = slot % len(TAGLINES)
    tagline = TAGLINES[idx]
    print(f"Slot {slot} -> tagline {idx + 1}/{len(TAGLINES)}: {tagline}")

    # 1) header template part: replace the .hdr-tag paragraph content
    resp = requests.get(
        f"{WP_URL}/wp-json/wp/v2/template-parts/phi-news//header?context=edit",
        auth=auth, timeout=20)
    resp.raise_for_status()
    content = resp.json()["content"]
    raw = content["raw"] if isinstance(content, dict) else content

    new_p = f'<p class="hdr-tag">{tagline_markup(tagline)}</p>'
    updated = re.sub(r'<p class="hdr-tag">.*?</p>', new_p, raw, count=1, flags=re.S)
    if updated == raw:
        print("No .hdr-tag paragraph found in header — skipping header update.")
    else:
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/template-parts/phi-news//header",
            auth=auth, json={"content": updated}, timeout=20)
        print("header:", r.status_code)

    # 2) site description (RSS/meta)
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/settings", auth=auth,
                      json={"description": tagline}, timeout=20)
    print("settings:", r.status_code)


if __name__ == "__main__":
    main()
