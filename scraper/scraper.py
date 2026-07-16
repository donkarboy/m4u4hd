"""
M4UHD Scraper — designed for GitHub Actions
Scrapes new-movies (pages 1–1847) and new-tv-series (pages 1–177).
Saves results to data/movies.json and data/series.json incrementally.

Env vars (set as GitHub Actions secrets/vars):
  SCRAPE_TYPE   : movies | series | both  (default: both)
  START_PAGE    : int  (default: 1)
  END_PAGE      : int  (default: last known page)
  DELAY_MIN     : float seconds between requests (default: 2.0)
  DELAY_MAX     : float (default: 4.0)
"""

import os
import sys
import json
import time
import random
import logging
from datetime import datetime, timezone
from pathlib import Path

import cloudscraper
from bs4 import BeautifulSoup

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("m4uhd")

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = "https://ww1.m4uhd.page"

ENDPOINTS = {
    "movies": {
        "path": "/new-movies",
        "last_page": 1847,
        "label": "New Movies",
        "output": "data/movies.json",
    },
    "series": {
        "path": "/new-tv-series",
        "last_page": 177,
        "label": "New TV Series",
        "output": "data/series.json",
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;"
        "q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Priority": "u=0, i",
}

STATIC_COOKIES = {"viewEposideob7cy": "i9767"}

DELAY_MIN = float(os.getenv("DELAY_MIN", "2.0"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "4.0"))

# Max consecutive errors before aborting a section
MAX_ERRORS = 10

# ── Helpers ───────────────────────────────────────────────────────────────────

def page_url(path: str, page: int) -> str:
    return BASE_URL + path if page == 1 else f"{BASE_URL}{path}?page={page}"


def parse_items(html: str, page: int, section: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []

    # Try selectors from most to least specific
    selectors = [
        "div.flw-item", "div.item", "article.item",
        "div.movie-item", "div.film-item", "div.card.item",
    ]
    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            break

    # Generic fallback: any link wrapping a poster image
    if not cards:
        for a in soup.select(
            "a[href*='/movie/'], a[href*='/tv/'], a[href*='/film/'], a[href*='/watch/']"
        ):
            if a.find("img"):
                cards.append(a)

    for card in cards:
        item: dict = {"page": page, "type": section, "scraped_at": datetime.now(timezone.utc).isoformat()}

        # Title & URL
        for sel in ["h2 a", "h3 a", ".film-name a", ".movie-name a", "a[title]", "a"]:
            el = card.select_one(sel) if hasattr(card, "select_one") else (card if card.name == "a" else None)
            if el and el.get("href"):
                item["title"] = (el.get("title") or el.get_text(strip=True)).strip()
                href = el["href"]
                item["url"] = href if href.startswith("http") else BASE_URL + href
                break

        # Poster (lazysizes uses data-src)
        img = card.select_one("img[data-src]") or card.select_one("img[src]")
        if img:
            item["poster"] = img.get("data-src") or img.get("src", "")

        # Metadata badges
        for badge in card.select(".fdi-item, .badge, .quality, .year, .duration"):
            t = badge.get_text(strip=True)
            if t.isdigit() and len(t) == 4:
                item.setdefault("year", t)
            elif t.upper() in {"HD", "CAM", "SD", "4K", "HDTV", "BLURAY", "1080P", "720P", "WEBDL"}:
                item.setdefault("quality", t)
            elif "m" in t.lower() or "h" in t.lower():
                item.setdefault("duration", t)

        # Rating
        r_el = card.select_one(".film-rating span, .rating, .score, .imdb")
        if r_el:
            item["rating"] = r_el.get_text(strip=True)

        if item.get("title") and item.get("url"):
            items.append(item)

    return items


def detect_last_page(html: str) -> int | None:
    """Read the real last page number from pagination in the HTML."""
    soup = BeautifulSoup(html, "lxml")
    nums = []
    for a in soup.select("ul.pagination a, .pagination a, nav a"):
        t = a.get_text(strip=True)
        if t.isdigit():
            nums.append(int(t))
        href = a.get("href", "")
        if "page=" in href:
            try:
                nums.append(int(href.split("page=")[-1].split("&")[0]))
            except ValueError:
                pass
    return max(nums) if nums else None


def load_existing(path: str) -> tuple[list, int]:
    """Load existing output file; return (items, max_page_scraped)."""
    p = Path(path)
    if not p.exists():
        return [], 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items", [])
        max_page = max((i.get("page", 0) for i in items), default=0)
        log.info(f"  Loaded {len(items)} existing items (max page scraped: {max_page})")
        return items, max_page
    except Exception as e:
        log.warning(f"  Could not load {path}: {e} — starting fresh")
        return [], 0


def save(path: str, items: list, section: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    out = {
        "section": section,
        "total_items": len(items),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    Path(path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Core Scraper ──────────────────────────────────────────────────────────────

def scrape_section(
    scraper: cloudscraper.CloudScraper,
    section: str,
    start_page: int,
    end_page: int | None,
    resume: bool = True,
) -> list[dict]:
    cfg = ENDPOINTS[section]
    path, label, output = cfg["path"], cfg["label"], cfg["output"]

    # Resume: load what we already have
    existing_items, max_scraped = load_existing(output) if resume else ([], 0)
    actual_start = max(start_page, max_scraped + 1) if resume and max_scraped else start_page
    actual_end = end_page or cfg["last_page"]

    if actual_start > actual_end:
        log.info(f"  {label} already fully scraped (up to page {max_scraped}). Nothing to do.")
        return existing_items

    log.info(f"\n{'='*60}")
    log.info(f"  {label}")
    log.info(f"  Pages {actual_start} → {actual_end}  |  {BASE_URL + path}?page=N")
    log.info(f"{'='*60}")

    all_items = list(existing_items)
    referrer = BASE_URL + path
    consecutive_errors = 0

    for page in range(actual_start, actual_end + 1):
        url = page_url(path, page)
        headers = {**HEADERS, "Referer": referrer}

        log.info(f"  [{section.upper()}] Page {page}/{actual_end}  →  {url}")

        try:
            resp = scraper.get(url, headers=headers, timeout=30)
        except Exception as e:
            log.warning(f"    ✗ Request error: {e}")
            consecutive_errors += 1
            if consecutive_errors >= MAX_ERRORS:
                log.error(f"    {MAX_ERRORS} consecutive errors — aborting section")
                break
            time.sleep(10)
            continue

        if resp.status_code == 429:
            wait = 60
            log.warning(f"    ⚠ Rate limited (429) — waiting {wait}s")
            time.sleep(wait)
            continue
        elif resp.status_code == 403:
            log.error(f"    ✗ 403 Forbidden — Cloudflare blocked. Saving progress and exiting.")
            save(output, all_items, section)
            break
        elif resp.status_code != 200:
            log.warning(f"    ✗ HTTP {resp.status_code} — skipping page {page}")
            consecutive_errors += 1
            if consecutive_errors >= MAX_ERRORS:
                log.error(f"    {MAX_ERRORS} consecutive errors — aborting section")
                break
            referrer = url
            time.sleep(3)
            continue

        consecutive_errors = 0
        html = resp.text

        # Detect actual last page from first page HTML
        if page == actual_start:
            detected = detect_last_page(html)
            if detected and detected != actual_end:
                log.info(f"    ℹ Detected last page from HTML: {detected} (was {actual_end})")
                actual_end = detected

        items = parse_items(html, page, section)
        log.info(f"    ✓ {len(items)} items")

        if not items:
            log.warning(f"    ⚠ No items on page {page} — site structure may have changed")

        all_items.extend(items)

        # Save after every page so GitHub Actions can commit partial results
        save(output, all_items, section)

        referrer = url
        if page < actual_end:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    log.info(f"\n  ✓ {label} complete — {len(all_items)} total items\n")
    return all_items


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    scrape_type = os.getenv("SCRAPE_TYPE", "both").lower()
    start_page  = int(os.getenv("START_PAGE", "1"))
    end_page    = int(os.getenv("END_PAGE", "0")) or None
    resume      = os.getenv("RESUME", "true").lower() != "false"

    sections = ["movies", "series"] if scrape_type == "both" else [scrape_type]

    log.info(f"M4UHD Scraper starting — sections: {sections}, start: {start_page}, end: {end_page or 'last'}, resume: {resume}")

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    for k, v in STATIC_COOKIES.items():
        scraper.cookies.set(k, v, domain="ww1.m4uhd.page")

    failed = []
    for section in sections:
        try:
            scrape_section(scraper, section, start_page, end_page, resume=resume)
        except Exception as e:
            log.error(f"Section '{section}' failed with unhandled error: {e}")
            failed.append(section)

    if failed:
        log.error(f"Failed sections: {failed}")
        sys.exit(1)

    log.info("All sections complete.")


if __name__ == "__main__":
    main()
