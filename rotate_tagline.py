"""Rotate the site tagline on every run (every 6 hours, plus manual).

Each run reads the index from tagline_index.txt, applies that tagline to
the header template part's .hdr-tag paragraph and the WP site
description, then writes (idx+1) back for the next run. The workflow
commits the file, so the position persists between runs and wraps at
the end of the list.
"""
import html
import os
import re
import sys

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

INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tagline_index.txt")


def _read_index():
    try:
        return int(open(INDEX_FILE).read().strip()) % len(TAGLINES)
    except Exception:
        return 0


def _write_index(idx):
    with open(INDEX_FILE, "w") as f:
        f.write(str(idx))


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
    idx = _read_index()
    tagline = TAGLINES[idx]
    print(f"Index {idx} -> tagline {idx + 1}/{len(TAGLINES)}: {tagline}")
    _write_index((idx + 1) % len(TAGLINES))

    # 1) header template part: replace the .hdr-tag paragraph content
    resp = requests.get(
        f"{WP_URL}/wp-json/wp/v2/template-parts/phi-news//header?context=edit",
        auth=auth, timeout=20)
    resp.raise_for_status()
    content = resp.json()["content"]
    raw = content["raw"] if isinstance(content, dict) else content

    new_p = f'<p class="hdr-tag">{tagline_markup(tagline)}</p>'
    if not re.search(r'<p[^>]*hdr-tag[^>]*>.*?</p>', raw, flags=re.S):
        print("No .hdr-tag paragraph found in header — skipping header update.")
    else:
        updated = re.sub(r'<p[^>]*hdr-tag[^>]*>.*?</p>', new_p, raw,
                         count=1, flags=re.S)
        if updated == raw:
            print("Tagline already current — header unchanged.")
        else:
            r = requests.post(
                f"{WP_URL}/wp-json/wp/v2/template-parts/phi-news//header",
                auth=auth, json={"content": updated}, timeout=20)
            print("header:", r.status_code)

    # 2) site description (RSS/meta)
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/settings", auth=auth,
                      json={"description": tagline}, timeout=20)
    print("settings:", r.status_code)
    print(f"Wrote next index {(idx + 1) % len(TAGLINES)} to {INDEX_FILE}")


if __name__ == "__main__":
    main()
