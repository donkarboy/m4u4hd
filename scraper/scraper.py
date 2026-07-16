"""
M4UHD Scraper — designed for GitHub Actions
Scrapes new-movies (pages 1–1847) and new-tv-series (pages 1–177).
Saves results to data/movies.json and data/series.json incrementally.

Env vars:
  SCRAPE_TYPE   : movies | series | both  (default: both)
  START_PAGE    : int  (default: 1)
  END_PAGE      : int  (default: last known page, 0 = auto)
  RESUME        : true | false  (default: true)
  DELAY_MIN     : float seconds between requests (default: 2.0)
  DELAY_MAX     : float (default: 4.0)

  Proxies are hardcoded in PROXY_LIST near the top of this file.
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
MAX_ERRORS = 10


# ── Proxy Helpers ─────────────────────────────────────────────────────────────

# Hardcoded proxy list — format: (host, port, user, password)
# Add/remove proxies here as needed.
PROXY_LIST = [
    ("31.59.20.176",   "6754", "xdwwyuwg", "5j97qhea02pz"),
    ("31.56.127.193",  "7684", "xdwwyuwg", "5j97qhea02pz"),
    ("45.38.107.97",   "6014", "xdwwyuwg", "5j97qhea02pz"),
    ("198.105.121.200","6462", "xdwwyuwg", "5j97qhea02pz"),
    ("64.137.96.74",   "6641", "xdwwyuwg", "5j97qhea02pz"),
    ("198.23.243.226", "6361", "xdwwyuwg", "5j97qhea02pz"),
    ("38.154.185.97",  "6370", "xdwwyuwg", "5j97qhea02pz"),
]


def load_proxies() -> list[dict]:
    """Build proxy dicts from the hardcoded PROXY_LIST above."""
    proxies = []
    for host, port, user, pw in PROXY_LIST:
        url = f"http://{user}:{pw}@{host}:{port}"
        proxies.append({"http": url, "https": url})
    log.info(f"Loaded {len(proxies)} hardcoded proxies")
    return proxies


class ProxyRotator:
    def __init__(self, proxies: list[dict]):
        self._proxies = proxies
        self._index = 0

    def get(self) -> dict | None:
        if not self._proxies:
            return None
        p = self._proxies[self._index % len(self._proxies)]
        return p

    def rotate(self):
        if self._proxies:
            self._index = (self._index + 1) % len(self._proxies)
            log.info(f"Rotated to proxy {self._index + 1}/{len(self._proxies)}")


# ── HTML Parsing ──────────────────────────────────────────────────────────────

def parse_items(html: str, page: int, section: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    items = []

    # Match what actually works on the site (div.item is the real selector)
    cards = (
        soup.select("div.item")
        or soup.select("article.item")
        or soup.select("div.flw-item")
        or soup.select("div.movie-item")
        or soup.select("div.film-item")
        or soup.select("div.card.item")
    )

    # Generic fallback: links wrapping poster images
    if not cards:
        for a in soup.select(
            "a[href*='/movie/'], a[href*='/tv/'], a[href*='/film/'], a[href*='/watch/']"
        ):
            if a.find("img"):
                cards.append(a)

    for card in cards:
        item: dict = {
            "page": page,
            "type": section,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

        # Title & URL
        title_el = (
            card.select_one("h2 a")
            or card.select_one("h3 a")
            or card.select_one(".film-name a")
            or card.select_one(".movie-name a")
            or card.select_one("a[title]")
            or card.select_one("a")
        )
        if title_el:
            item["title"] = (title_el.get("title") or title_el.get_text(strip=True)).strip()
            href = title_el.get("href", "")
            item["url"] = href if href.startswith("http") else BASE_URL + href

        # Poster
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


# ── Persistence ───────────────────────────────────────────────────────────────

def load_existing(path: str) -> tuple[list, int]:
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

def make_scraper(proxy: dict | None = None) -> cloudscraper.CloudScraper:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    for k, v in STATIC_COOKIES.items():
        scraper.cookies.set(k, v, domain="ww1.m4uhd.page")
    if proxy:
        scraper.proxies.update(proxy)
    return scraper


def scrape_section(
    rotator: ProxyRotator,
    section: str,
    start_page: int,
    end_page: int | None,
    resume: bool = True,
) -> list[dict]:
    cfg = ENDPOINTS[section]
    path, label, output = cfg["path"], cfg["label"], cfg["output"]

    existing_items, max_scraped = load_existing(output) if resume else ([], 0)
    actual_start = max(start_page, max_scraped + 1) if resume and max_scraped else start_page
    actual_end = end_page or cfg["last_page"]

    if actual_start > actual_end:
        log.info(f"  {label} already fully scraped (up to page {max_scraped}). Nothing to do.")
        return existing_items

    log.info(f"\n{'='*60}")
    log.info(f"  {label}")
    log.info(f"  Pages {actual_start} → {actual_end}")
    log.info(f"{'='*60}")

    all_items = list(existing_items)
    referrer = BASE_URL + path
    consecutive_errors = 0

    # Build initial scraper with first proxy (if any)
    scraper = make_scraper(rotator.get())

    for page in range(actual_start, actual_end + 1):
        url = f"{BASE_URL}{path}" if page == 1 else f"{BASE_URL}{path}?page={page}"
        headers = {**HEADERS, "Referer": referrer}

        log.info(f"  [{section.upper()}] Page {page}/{actual_end}  →  {url}")

        try:
            resp = scraper.get(url, headers=headers, timeout=30)
        except Exception as e:
            log.warning(f"    ✗ Request error: {e}")
            consecutive_errors += 1
            # Rotate proxy on connection errors
            rotator.rotate()
            scraper = make_scraper(rotator.get())
            if consecutive_errors >= MAX_ERRORS:
                log.error(f"    {MAX_ERRORS} consecutive errors — aborting section")
                break
            time.sleep(10)
            continue

        if resp.status_code == 429:
            log.warning(f"    ⚠ Rate limited (429) — rotating proxy and waiting 30s")
            rotator.rotate()
            scraper = make_scraper(rotator.get())
            time.sleep(30)
            continue

        elif resp.status_code == 403:
            log.warning(f"    ✗ 403 Forbidden — rotating proxy")
            rotator.rotate()
            scraper = make_scraper(rotator.get())
            consecutive_errors += 1
            if consecutive_errors >= MAX_ERRORS:
                log.error(f"    Too many 403s — aborting section")
                save(output, all_items, section)
                break
            time.sleep(5)
            continue

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
        save(output, all_items, section)

        referrer = url

        # Rotate proxy every 50 pages to spread load
        if rotator._proxies and page % 50 == 0:
            rotator.rotate()
            scraper = make_scraper(rotator.get())

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

    proxies = load_proxies()
    rotator = ProxyRotator(proxies)

    if proxies:
        log.info(f"Using {len(proxies)} proxies with rotation every 50 pages")
    else:
        log.info("No proxies configured — using direct connection")

    log.info(f"M4UHD Scraper starting — sections: {sections}, start: {start_page}, "
             f"end: {end_page or 'last'}, resume: {resume}")

    failed = []
    for section in sections:
        try:
            scrape_section(rotator, section, start_page, end_page, resume=resume)
        except Exception as e:
            log.error(f"Section '{section}' failed with unhandled error: {e}")
            failed.append(section)

    if failed:
        log.error(f"Failed sections: {failed}")
        sys.exit(1)

    log.info("All sections complete.")


if __name__ == "__main__":
    main()
