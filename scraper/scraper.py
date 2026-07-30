"""
M4UHD Scraper — fixed version
Key fixes:
  1. Broadened + corrected CSS selectors for the actual site structure
  2. Added --debug flag to dump raw HTML and discovered classes on page 1
  3. Fixed Chrome UA version (124, not 150 which doesn't exist)
  4. Smarter fallback: every <a> wrapping an <img> is a candidate card
  5. Title extraction now also checks .film-name, .movie-name, data-title, alt text
  6. Added SCRAPE_DEBUG env var to save page HTML when 0 items found
  7. Updated proxy credentials (9 proxies, glsbcfvl / 336gxb0or4n9)
  8. FIXED: Proxy failure detection — scraper no longer silently falls back to direct IP
  9. FIXED: Cloudflare challenge detection — retries with next proxy instead of parsing junk HTML
"""

import os
import sys
import json
import time
import random
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
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

DELAY_MIN     = float(os.getenv("DELAY_MIN", "2.0"))
DELAY_MAX     = float(os.getenv("DELAY_MAX", "4.0"))
MAX_ERRORS    = 10
SCRAPE_DEBUG  = os.getenv("SCRAPE_DEBUG", "false").lower() == "true"

# ── Cloudflare challenge detection ────────────────────────────────────────────
# These strings appear in Cloudflare JS-challenge / IUAM pages
CF_MARKERS = [
    "cf-browser-verification",
    "checking your browser",
    "enable javascript",
    "cf_clearance",
    "jschl-answer",
    "ray id",
    "__cf_chl",
    "turnstile",
    "challenges.cloudflare.com",
]

def is_cloudflare_challenge(html: str) -> bool:
    lower = html.lower()
    return sum(1 for m in CF_MARKERS if m in lower) >= 2


# ── Proxy List ────────────────────────────────────────────────────────────────
PROXY_LIST = [
    ("31.59.20.176",    "6754", "glsbcfvl", "336gxb0or4n9"),  # UK, London
    ("31.56.127.193",   "7684", "glsbcfvl", "336gxb0or4n9"),  # US, Seattle
    ("45.38.107.97",    "6014", "glsbcfvl", "336gxb0or4n9"),  # UK, London
    ("198.105.121.200", "6462", "glsbcfvl", "336gxb0or4n9"),  # UK, London
    ("64.137.96.74",    "6641", "glsbcfvl", "336gxb0or4n9"),  # ES, Madrid
    ("198.23.243.226",  "6361", "glsbcfvl", "336gxb0or4n9"),  # US, Los Angeles
    ("38.154.185.97",   "6370", "glsbcfvl", "336gxb0or4n9"),  # US, Piscataway
    ("84.247.60.125",   "6095", "glsbcfvl", "336gxb0or4n9"),  # PL, Warsaw
    ("142.111.67.146",  "5611", "glsbcfvl", "336gxb0or4n9"),  # Working
]


def load_proxies() -> list[dict]:
    proxies = []
    for host, port, user, pw in PROXY_LIST:
        url = f"http://{user}:{pw}@{host}:{port}"
        proxies.append({"http": url, "https": url, "_label": f"{host}:{port}"})
    log.info(f"Loaded {len(proxies)} hardcoded proxies")
    return proxies


class ProxyRotator:
    def __init__(self, proxies: list[dict]):
        self._proxies = proxies
        self._index   = 0
        self._bad: set[int] = set()   # indexes of confirmed-dead proxies

    def get(self) -> dict | None:
        if not self._proxies:
            return None
        # Skip proxies we know are dead
        for _ in range(len(self._proxies)):
            p = self._proxies[self._index % len(self._proxies)]
            if self._index % len(self._proxies) not in self._bad:
                return p
            self._index += 1
        return self._proxies[0]  # all dead — use first anyway

    def rotate(self, mark_bad: bool = False):
        if not self._proxies:
            return
        if mark_bad:
            self._bad.add(self._index % len(self._proxies))
            log.warning(f"    Marked proxy {self._index % len(self._proxies) + 1} as dead "
                        f"({len(self._bad)}/{len(self._proxies)} dead)")
        self._index = (self._index + 1) % len(self._proxies)
        label = self._proxies[self._index % len(self._proxies)].get("_label", "?")
        log.info(f"    Rotated to proxy {self._index % len(self._proxies) + 1}"
                 f"/{len(self._proxies)}: {label}")

    @property
    def all_dead(self) -> bool:
        return len(self._bad) >= len(self._proxies)


def make_scraper(proxy: dict | None = None) -> cloudscraper.CloudScraper:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    for k, v in STATIC_COOKIES.items():
        scraper.cookies.set(k, v, domain="ww1.m4uhd.page")
    if proxy:
        # Strip internal _label key before passing to requests
        clean = {k: v for k, v in proxy.items() if not k.startswith("_")}
        scraper.proxies.update(clean)
    return scraper


def fetch_with_proxy(scraper: cloudscraper.CloudScraper,
                     url: str,
                     headers: dict,
                     proxy: dict | None) -> requests.Response | None:
    """
    Fetch URL.  Returns Response on success, None on proxy/connection failure.
    Raises on non-connection errors so the caller can handle HTTP status codes.
    """
    try:
        resp = scraper.get(url, headers=headers, timeout=30)
        return resp
    except (requests.exceptions.ProxyError,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ConnectionError) as e:
        label = proxy.get("_label", "direct") if proxy else "direct"
        log.warning(f"    ✗ Proxy {label} unreachable: {type(e).__name__}")
        return None
    except Exception as e:
        log.warning(f"    ✗ Request error: {e}")
        return None


# ── Debug helper ──────────────────────────────────────────────────────────────

def debug_html_structure(html: str, label: str = "debug"):
    soup = BeautifulSoup(html, "lxml")

    log.info(f"\n{'─'*60}")
    log.info(f"  DEBUG STRUCTURE for: {label}")
    log.info(f"  Page length: {len(html)} chars")

    if is_cloudflare_challenge(html):
        log.warning("  ⚠️  THIS IS A CLOUDFLARE CHALLENGE PAGE — proxy is not working!")
        log.info(f"{'─'*60}\n")
        return

    classes: dict[str, int] = {}
    for tag in soup.find_all(["div", "article", "li", "section", "ul"], class_=True):
        for c in tag.get("class", []):
            classes[c] = classes.get(c, 0) + 1
    top = sorted(classes.items(), key=lambda x: -x[1])[:40]
    log.info("  Top classes: " + ", ".join(f".{c}({n})" for c, n in top))

    links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if any(x in href for x in ["/movie", "/watch", "/film", "/tv", "/series", "/show"]):
            img = a.find("img")
            txt = a.get_text(strip=True)[:40]
            links.append(f"href={href!r} img={'yes' if img else 'no'} text={txt!r}")
    log.info(f"  Content links found: {len(links)}")
    for lnk in links[:10]:
        log.info(f"    {lnk}")

    if SCRAPE_DEBUG:
        out = Path("debug_last_page.html")
        out.write_text(html, encoding="utf-8")
        log.info(f"  Saved raw HTML → {out.resolve()}")

    log.info(f"{'─'*60}\n")


# ── HTML Parsing ──────────────────────────────────────────────────────────────

CARD_SELECTORS = [
    "div.flw-item",
    "div.film_list-wrap .film-poster",
    "div.item",
    "article.item",
    "div.movie-item",
    "div.film-item",
    "div.card.item",
    "div.movie_list li",
    "div.ml-item",
    "div.ml-mask",
    "ul.movie-list li",
    "div.content-item",
    "div.thumb",
    "li.post-item",
    "article",
]


def _extract_from_card(card, page: int, section: str) -> dict | None:
    item: dict = {
        "page": page,
        "type": section,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    title_el = (
        card.select_one("h2 a") or card.select_one("h3 a") or card.select_one("h4 a")
        or card.select_one(".film-name a") or card.select_one(".movie-name a")
        or card.select_one(".title a") or card.select_one("a[title]")
        or card.select_one("a")
    )
    if title_el:
        title = (
            title_el.get("title") or title_el.get("data-title")
            or title_el.get_text(strip=True)
        ).strip()
        if title:
            item["title"] = title
        href = title_el.get("href", "")
        item["url"] = href if href.startswith("http") else BASE_URL + href

    if not item.get("url") and card.name == "a":
        href = card.get("href", "")
        item["url"] = href if href.startswith("http") else BASE_URL + href
        img = card.find("img")
        if img and not item.get("title"):
            item["title"] = (img.get("alt") or "").strip()

    img = (
        card.select_one("img[data-src]") or card.select_one("img[data-original]")
        or card.select_one("img[src]")
    )
    if img:
        item["poster"] = (
            img.get("data-src") or img.get("data-original") or img.get("src", "")
        )

    for badge in card.select(
        ".fdi-item, .badge, .quality, .year, .duration, "
        ".film-detail-fix, .pick, .type, span"
    ):
        t = badge.get_text(strip=True)
        if not t:
            continue
        if t.isdigit() and len(t) == 4:
            item.setdefault("year", t)
        elif t.upper() in {"HD","CAM","SD","4K","HDTV","BLURAY","1080P","720P","480P","WEBDL","WEBRIP","HDRIP"}:
            item.setdefault("quality", t.upper())
        elif any(u in t.lower() for u in ["min", "h ", "hr", "episode"]):
            item.setdefault("duration", t)

    r_el = card.select_one(
        ".film-rating span, .rating, .score, .imdb, .star, "
        "[class*='rating'], [class*='score']"
    )
    if r_el:
        item["rating"] = r_el.get_text(strip=True)

    if not item.get("url"):
        return None
    url = item["url"]
    if url in (BASE_URL, BASE_URL + "/", "/", "") or "javascript" in url:
        return None
    return item


def parse_items(html: str, page: int, section: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = []

    for sel in CARD_SELECTORS:
        found = soup.select(sel)
        if found:
            log.debug(f"    Selector matched: {sel!r} → {len(found)} cards")
            cards = found
            break

    if not cards:
        for a in soup.find_all("a", href=True):
            if a.find("img"):
                href = a.get("href", "")
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
    actual_end   = end_page or cfg["last_page"]

    if actual_start > actual_end:
        log.info(f"  {label} already fully scraped (up to page {max_scraped}).")
        return existing_items

    log.info(f"\n{'='*60}")
    log.info(f"  {label}  |  Pages {actual_start} → {actual_end}")
    log.info(f"{'='*60}")

    all_items        = list(existing_items)
    referrer         = BASE_URL + path
    consecutive_errors = 0
    zero_item_streak = 0
    page             = actual_start

    # Build initial scraper
    proxy   = rotator.get()
    scraper = make_scraper(proxy)

    while page <= actual_end:
        url     = f"{BASE_URL}{path}" if page == 1 else f"{BASE_URL}{path}?page={page}"
        headers = {**HEADERS, "Referer": referrer}

        log.info(f"  [{section.upper()}] Page {page}/{actual_end}  →  {url}  "
                 f"[proxy: {proxy.get('_label','direct') if proxy else 'direct'}]")

        resp = fetch_with_proxy(scraper, url, headers, proxy)

        # ── Proxy connection failed ───────────────────────────────────────────
        if resp is None:
            rotator.rotate(mark_bad=True)
            if rotator.all_dead:
                log.error("    All proxies are unreachable — cannot continue.")
                log.error("    Your proxy credentials or IPs may have expired.")
                save(output, all_items, section)
                break
            proxy   = rotator.get()
            scraper = make_scraper(proxy)
            time.sleep(5)
            continue  # retry same page with next proxy

        # ── HTTP error handling ───────────────────────────────────────────────
        if resp.status_code == 429:
            log.warning("    ⚠ Rate limited (429) — rotating proxy, waiting 30s")
            rotator.rotate()
            proxy   = rotator.get()
            scraper = make_scraper(proxy)
            time.sleep(30)
            continue
        elif resp.status_code == 403:
            log.warning("    ✗ 403 Forbidden — rotating proxy")
            rotator.rotate(mark_bad=True)
            proxy   = rotator.get()
            scraper = make_scraper(proxy)
            consecutive_errors += 1
            if consecutive_errors >= MAX_ERRORS:
                log.error("    Too many errors — aborting section")
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
            page    += 1
            time.sleep(3)
            continue

        html = resp.text

        # ── Cloudflare challenge detection ────────────────────────────────────
        if is_cloudflare_challenge(html):
            log.warning(f"    ⚠ Cloudflare challenge detected on page {page} "
                        f"(proxy {proxy.get('_label','?') if proxy else 'direct'} not working)")
            rotator.rotate(mark_bad=True)
            if rotator.all_dead:
                log.error("    All proxies return Cloudflare challenge — cannot bypass.")
                log.error("    Proxies may be blocked by the site. Update your proxy list.")
                save(output, all_items, section)
                break
            proxy   = rotator.get()
            scraper = make_scraper(proxy)
            log.info("    Retrying same page with next proxy...")
            time.sleep(10)
            continue  # retry same page

        consecutive_errors = 0

        # Auto-detect real last page
        if page == actual_start:
            detected = detect_last_page(html)
            if detected and detected != actual_end:
                log.info(f"    ℹ Detected last page from HTML: {detected} (was {actual_end})")
                actual_end = detected

        if debug_first_page and page == actual_start:
            debug_html_structure(html, label=f"{section} page {page}")

        items = parse_items(html, page, section)
        log.info(f"    ✓ {len(items)} items")

        if not items:
            zero_item_streak += 1
            log.warning(f"    ⚠ No items on page {page} (streak: {zero_item_streak})")
            if zero_item_streak <= 2:
                debug_html_structure(html, label=f"{section} page {page} [EMPTY]")
            if zero_item_streak >= 5:
                log.error("    5 consecutive empty pages — aborting. "
                          "Set SCRAPE_DEBUG=true and inspect debug_last_page.html")
                save(output, all_items, section)
                break
        else:
            zero_item_streak = 0
            all_items.extend(items)
            save(output, all_items, section)

        referrer = url
        page    += 1

        # Rotate proxy every 50 pages to spread load
        if rotator._proxies and page % 50 == 0:
            rotator.rotate()
            proxy   = rotator.get()
            scraper = make_scraper(proxy)

        if page <= actual_end:
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
            log.error(f"Section '{section}' failed: {e}", exc_info=True)
            failed.append(section)

    if failed:
        log.error(f"Failed sections: {failed}")
        sys.exit(1)

    log.info("All sections complete.")


if __name__ == "__main__":
    main()
