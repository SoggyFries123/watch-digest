#!/usr/bin/env python3
"""Fetch the watch-press RSS feeds server-side and write feeds.json.

Runs inside a GitHub Action (see .github/workflows/update-feeds.yml).
Standard library only, so there is nothing to pip install.
"""
import json
import re
import sys
import time
import html
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SOURCES = [
    ("A Timely Perspective", "https://atimelyperspective.com/feed/"),
    ("Deployant",            "https://deployant.com/feed/"),
    ("Fratello Watches",     "https://www.fratellowatches.com/feed/"),
    ("Monochrome Watches",   "https://monochrome-watches.com/blog/feed/"),
    ("Worn & Wound",         "https://www.wornandwound.com/feed"),
    ("Quill & Pad",          "https://quillandpad.com/feed/"),
    ("Revolution",           "https://revolutionwatch.com/feed/"),
    ("Time and Watches",     "https://www.timeandwatches.com/feeds/posts/default?alt=rss"),
    ("Time+Tide Watches",    "https://timeandtidewatches.com/feed/"),
]

OUTPUT = Path(__file__).parent / "feeds.json"
USER_AGENT = "Mozilla/5.0 (compatible; WatchDigestBot/1.0)"
TIMEOUT = 20
MAX_PER_SOURCE = 40      # newest items kept per source (older ones roll off)
OG_FETCH_LIMIT = 10      # max article pages opened per source per run, for missing images

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}
BAD_IMAGE_HINTS = ("emoji", "pixel", "gravatar", "spacer", "blank.gif", "1x1")


def http_get(url, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read(limit) if limit else resp.read()


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def good_image(url):
    if not url or not url.startswith(("http://", "https://")):
        return False
    low = url.lower()
    return not any(h in low for h in BAD_IMAGE_HINTS)


def upgrade_blogger(url):
    # Blogger feeds hand out 72px thumbnails; ask for a bigger rendition.
    if "blogspot" in url or "googleusercontent" in url:
        return re.sub(r"/s\d+(-c)?/", "/s640/", url)
    return url


def image_from_element(item):
    # 1. Media RSS / enclosure tags
    for el in item.iter():
        tag = el.tag.split("}")[-1]
        url = el.get("url")
        if tag in ("content", "thumbnail") and url and good_image(url):
            medium = (el.get("medium") or el.get("type") or "image").lower()
            if medium.startswith("image") or tag == "thumbnail":
                return upgrade_blogger(url)
        if tag == "enclosure" and url and (el.get("type") or "").startswith("image") and good_image(url):
            return url
    # 2. First <img> inside the post body / summary
    for path in ("content:encoded", "description", "atom:content", "atom:summary"):
        body = item.findtext(path, namespaces=NS)
        if body:
            for m in re.finditer(r"<img[^>]+(?:src|data-src)=[\"']([^\"']+)[\"']", body, re.I):
                url = html.unescape(m.group(1))
                if good_image(url):
                    return upgrade_blogger(url)
    return None


def parse_feed(xml_bytes, source_name):
    root = ET.fromstring(xml_bytes)
    items = []
    entries = list(root.iter("item")) or list(root.iter("{%s}entry" % NS["atom"]))
    for e in entries:
        title = html.unescape((e.findtext("title") or e.findtext("atom:title", namespaces=NS) or "").strip())
        link = (e.findtext("link") or "").strip()
        if not link:  # Atom style <link href="...">
            for l in e.findall("atom:link", NS):
                if l.get("rel", "alternate") == "alternate" and l.get("href"):
                    link = l.get("href")
                    break
        date = parse_date(e.findtext("pubDate") or e.findtext("atom:published", namespaces=NS)
                          or e.findtext("atom:updated", namespaces=NS)
                          or e.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "date": date.isoformat() if date else None,
            "image": image_from_element(e),
            "source": source_name,
        })
    return items


def og_image(article_url):
    """Return (image_or_None, page_was_fetched)."""
    try:
        page = http_get(article_url, limit=400_000).decode("utf-8", errors="ignore")
    except Exception:
        return None, False
    for pat in (
        r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:image[\"']",
        r"<meta[^>]+name=[\"']twitter:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
    ):
        m = re.search(pat, page, re.I)
        if m:
            url = html.unescape(m.group(1))
            if good_image(url):
                return url, True
    return None, True


def sort_key(item):
    return item["date"] or ""


def merge_source(name, fresh, previous):
    prev_by_link = {p["link"]: p for p in previous}
    for it in fresh:
        old = prev_by_link.get(it["link"])
        if old:
            if not it["image"] and old.get("image"):
                it["image"] = old["image"]
            if old.get("og_tried"):
                it["og_tried"] = True
    fresh_links = {it["link"] for it in fresh}
    carried = [p for p in previous if p["link"] not in fresh_links]
    merged = sorted(fresh + carried, key=sort_key, reverse=True)[:MAX_PER_SOURCE]

    opened = 0
    for it in merged:  # fill missing images from the article page itself
        if it.get("image") or it.get("og_tried") or opened >= OG_FETCH_LIMIT:
            continue
        opened += 1
        img, fetched = og_image(it["link"])
        if img:
            it["image"] = img
        elif fetched:
            it["og_tried"] = True
        time.sleep(0.5)
    return merged


def main():
    previous = {}
    prev_status = {}
    if OUTPUT.exists():
        try:
            old = json.loads(OUTPUT.read_text(encoding="utf-8"))
            for it in old.get("items", []):
                previous.setdefault(it["source"], []).append(it)
            prev_status = old.get("sources", {})
        except (ValueError, KeyError):
            pass

    all_items, status, ok_count = [], {}, 0
    now = datetime.now(timezone.utc).isoformat()
    for name, url in SOURCES:
        try:
            fresh = parse_feed(http_get(url), name)
            if not fresh:
                raise ValueError("feed parsed but contained no items")
            merged = merge_source(name, fresh, previous.get(name, []))
            status[name] = {"ok": True, "fetched": len(fresh), "total": len(merged),
                            "last_success": now, "error": None}
            ok_count += 1
            print(f"OK    {name}: {len(fresh)} fetched, {len(merged)} kept")
        except Exception as exc:  # keep whatever we had before for this source
            merged = previous.get(name, [])
            status[name] = {"ok": False, "fetched": 0, "total": len(merged),
                            "last_success": prev_status.get(name, {}).get("last_success"),
                            "error": f"{type(exc).__name__}: {exc}"[:200]}
            print(f"FAIL  {name}: {exc}", file=sys.stderr)
        all_items.extend(merged)

    all_items.sort(key=sort_key, reverse=True)
    OUTPUT.write_text(json.dumps(
        {"generated_at": now, "sources": status, "items": all_items},
        ensure_ascii=False, indent=1), encoding="utf-8")

    if ok_count == 0:
        print("Every source failed - failing the run so you get notified.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
