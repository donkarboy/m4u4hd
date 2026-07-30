"""
M4UHD Scraper — fixed version
Key fixes:
  1. Broadened + corrected CSS selectors for the actual site structure
  2. Added --debug flag to dump raw HTML and discovered classes on page 1
  3. Fixed Chrome UA version (124, not 150 which doesn't exist)
  4. Smarter fallback: every <a> wrapping an <img> is a candidate card
  5. Title extraction now also checks .film-name, .movie-name, data-title, alt text
  6. Added SCRAPE_DEBUG env var to save page HTML when 0 items found
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

# Real Chrome 124 UA — Chrome 150 doesn't exist and may trigger bot detection
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
}

STATIC_COOKIES = {"viewEposideob7cy": "i9767"}

DELAY_MIN = float(os.getenv("DELAY_MIN", "2.0"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "4.0"))
MAX_ERRORS = 10
SCRAPE_DEBUG = os.getenv("SCRAPE_DEBUG", "false").lower() == "true"

# ── Proxy Helpers ─────────────────────────────────────────────────────────────
PROXY_LIST = [
    ("31.59.20.176",    "6754", "xdwwyuwg", "5j97qhea02pz"),
    ("31.56.127.193",   "7684", "xdwwyuwg", "5j97qhea02pz"),
    ("45.38.107.97",    "6014", "xdwwyuwg", "5j97qhea02pz"),
    ("198.105.121.200", "6462", "xdwwyuwg", "5j97qhea02pz"),
    ("64.137.96.74",    "6641", "xdwwyuwg", "5j97qhea02pz"),
    ("198.23.243.226",  "6361", "xdwwyuwg", "5j97qhea02pz"),
    ("38.154.185.97",   "6370", "xdwwyuwg", "5j97qhea02pz"),
]


def load_proxies() -> list[dict]:
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
        return self._proxies[self._index % len(self._proxies)]

    def rotate(self):
        if self._proxies:
            self._index = (self._index + 1) % len(self._proxies)
            log.info(f"Rotated to proxy {self._index + 1}/{len(self._proxies)}")


# ── Debug helper ──────────────────────────────────────────────────────────────

def debug_html_structure(html: str, label: str = "debug"):
    """Log the actual classes and link structure found in the page."""
    soup = BeautifulSoup(html, "lxml")

    log.info(f"\n{'─'*60}")
    log.info(f"  DEBUG STRUCTURE for: {label}")
    log.info(f"  Page length: {len(html)} chars")

    # Top 40 classes by frequency
    classes: dict[str, int] = {}
    for tag in soup.find_all(["div", "article", "li", "section", "ul"], class_=True):
        for c in tag.get("class", []):
            classes[c] = classes.get(c, 0) + 1
    top = sorted(classes.items(), key=lambda x: -x[1])[:40]
    log.info("  Top classes: " + ", ".join(f".{c}({n})" for c, n in top))

    # Any links that look like content
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if any(x in href for x in ["/movie", "/watch", "/film", "/tv", "/series", "/show"]):
            img = a.find("img")
            txt = a.get_text(strip=True)[:40]
            links.append(f"href={href!r} img={'yes' if img else 'no'} text={txt!r}")
    log.info(f"  Content links found: {len(links)}")
    for l in links[:10]:
        log.info(f"    {l}")

    # Save to disk for manual inspection
    if SCRAPE_DEBUG:
        out = Path("debug_last_page.html")
        out.write_text(html, encoding="utf-8")
        log.info(f"  Saved raw HTML → {out.resolve()}")

    log.info(f"{'─'*60}\n")


# ── HTML Parsing ──────────────────────────────────────────────────────────────

# Ordered from most-specific to broadest.
# Add new selectors at the TOP if you discover a better one via debug output.
CARD_SELECTORS = [
    # Site-specific (update these after running --debug once)
    "div.flw-item",
    "div.film_list-wrap .film-poster",   # flixhq / similar family
    "div.item",
    "article.item",
    "div.movie-item",
    "div.film-item",
    "div.card.item",
    "div.movie_list li",
    "div.ml-item",                        # common on m4u family
    "div.ml-mask",
    "ul.movie-list li",
    "div.content-item",
    "div.thumb",
    # Generic: any list-item or article wrapping a poster image
    "li.post-item",
    "article",
]

def _extract_from_card(card, page: int, section: str) -> dict | None:
    """Extract a single item from a card element."""
    item: dict = {
        "page": page,
        "type": section,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    # ── Title & URL ──────────────────────────────────────────────────────────
    title_el = (
        card.select_one("h2 a")
        or card.select_one("h3 a")
        or card.select_one("h4 a")
        or card.select_one(".film-name a")
        or card.select_one(".movie-name a")
        or card.select_one(".title a")
        or card.select_one("a[title]")
        or card.select_one("a")          # last resort
    )
    if title_el:
        title = (
            title_el.get("title")
            or title_el.get("data-title")
            or title_el.get_text(strip=True)
        ).strip()
        if title:
            item["title"] = title
        href = title_el.get("href", "")
        item["url"] = href if href.startswith("http") else BASE_URL + href

    # If the card itself is an <a> tag (generic fallback)
    if not item.get("url") and card.name == "a":
        href = card.get("href", "")
        item["url"] = href if href.startswith("http") else BASE_URL + href
        img = card.find("img")
        if img and not item.get("title"):
            item["title"] = (img.get("alt") or "").strip()

    # ── Poster ───────────────────────────────────────────────────────────────
    img = (
        card.select_one("img[data-src]")
        or card.select_one("img[data-original]")
        or card.select_one("img[src]")
    )
    if img:
        item["poster"] = (
            img.get("data-src")
            or img.get("data-original")
            or img.get("src", "")
        )

    # ── Metadata badges ───────────────────────────────────────────────────────
    for badge in card.select(
        ".fdi-item, .badge, .quality, .year, .duration, "
        ".film-detail-fix, .pick, .type, span"
    ):
        t = badge.get_text(strip=True)
        if not t:
            continue
        if t.isdigit() and len(t) == 4:
            item.setdefault("year", t)
        elif t.upper() in {
            "HD", "CAM", "SD", "4K", "HDTV", "BLURAY",
            "1080P", "720P", "480P", "WEBDL", "WEBRIP", "HDRIP",
        }:
            item.setdefault("quality", t.upper())
        elif any(u in t.lower() for u in ["min", "h ", "hr", "episode"]):
            item.setdefault("duration", t)

    # ── Rating ────────────────────────────────────────────────────────────────
    r_el = card.select_one(
        ".film-rating span, .rating, .score, .imdb, "
        ".star, [class*='rating'], [class*='score']"
    )
    if r_el:
        item["rating"] = r_el.get_text(strip=True)

    # Must have at minimum a URL
    if not item.get("url"):
        return None
    # Filter out obviously wrong URLs
    url = item["url"]
    if url in (BASE_URL, BASE_URL + "/", "/", "") or "javascript" in url:
        return None

    return item


def parse_items(html: str, page: int, section: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = []

    # Try each selector in order; use the first one that yields results
    for sel in CARD_SELECTORS:
        found = soup.select(sel)
        if found:
            log.debug(f"    Selector matched: {sel!r} → {len(found)} cards")
            cards = found
            break

    # Ultimate fallback: every <a> that wraps an <img>
    if not cards:
        for a in soup.find_all("a", href=True):
            if a.find("img"):
                href = a.get("href", "")
                # Only include if the href looks like a content page
                if any(x in href for x in ["/movie", "/watch", "/film", "/tv", "/series", "/show"]):
                    cards.append(a)

    items = []
    for card in cards:
        item = _extract_from_card(card, page, section)
        if item and item.get("title"):
            items.append(item)

    return items


def detect_last_page(html: str) -> int | None:
    soup = BeautifulSoup(html, "lxml")
    nums = []
    for a in soup.select("ul.pagination a, .pagination a, nav a, .page-link"):
        t = a.get_text(strip=True)
        if t.isdigit():
            nums.append(int(t))
        href = a.get("href", "")
        for param in ["page=", "p="]:
            if param in href:
                try:
                    nums.append(int(href.split(param)[-1].split("&")[0].split("/")[0]))
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
    debug_first_page: bool = False,
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
    zero_item_streak = 0          # NEW: track how many pages in a row gave 0 items

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
            rotator.rotate()
            scraper = make_scraper(rotator.get())
            if consecutive_errors >= MAX_ERRORS:
                log.error(f"    {MAX_ERRORS} consecutive errors — aborting section")
                break
            time.sleep(10)
            continue

        if resp.status_code == 429:
            log.warning("    ⚠ Rate limited (429) — rotating proxy and waiting 30s")
            rotator.rotate()
            scraper = make_scraper(rotator.get())
            time.sleep(30)
            continue
        elif resp.status_code == 403:
            log.warning("    ✗ 403 Forbidden — rotating proxy")
            rotator.rotate()
            scraper = make_scraper(rotator.get())
            consecutive_errors += 1
            if consecutive_errors >= MAX_ERRORS:
                log.error("    Too many 403s — aborting section")
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

        # Auto-detect last page from pagination on first page
        if page == actual_start:
            detected = detect_last_page(html)
            if detected and detected != actual_end:
                log.info(f"    ℹ Detected last page from HTML: {detected} (was {actual_end})")
                actual_end = detected

        # Debug: dump structure on first page or when asked
        if debug_first_page and page == actual_start:
            debug_html_structure(html, label=f"{section} page {page}")

        items = parse_items(html, page, section)
        log.info(f"    ✓ {len(items)} items")

        if not items:
            zero_item_streak += 1
            log.warning(f"    ⚠ No items on page {page} — site structure may have changed "
                        f"(streak: {zero_item_streak})")
            # Dump HTML structure after 2 consecutive empty pages to help debug
            if zero_item_streak <= 2:
                debug_html_structure(html, label=f"{section} page {page} [EMPTY]")
            # After 5 empty pages, abort — something is wrong
            if zero_item_streak >= 5:
                log.error("    5 consecutive pages with 0 items — aborting. "
                          "Run with SCRAPE_DEBUG=true and check debug_last_page.html")
                save(output, all_items, section)
                break
        else:
            zero_item_streak = 0
            all_items.extend(items)
            save(output, all_items, section)

        referrer = url

        # Rotate proxy every 50 pages
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
    debug       = "--debug" in sys.argv or SCRAPE_DEBUG

    sections = ["movies", "series"] if scrape_type == "both" else [scrape_type]

    proxies = load_proxies()
    rotator = ProxyRotator(proxies)

    log.info(f"M4UHD Scraper starting — sections: {sections}, start: {start_page}, "
             f"end: {end_page or 'last'}, resume: {resume}, debug: {debug}")

    failed = []
    for section in sections:
        try:
            scrape_section(rotator, section, start_page, end_page,
                           resume=resume, debug_first_page=debug)
        except Exception as e:
            log.error(f"Section '{section}' failed with unhandled error: {e}", exc_info=True)
            failed.append(section)

    if failed:
        log.error(f"Failed sections: {failed}")
        sys.exit(1)

    log.info("All sections complete.")


if __name__ == "__main__":
    main()
