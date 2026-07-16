import os
import sys
import json
import time
import random
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

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

DELAY_MIN  = float(os.getenv("DELAY_MIN", "2.0"))
DELAY_MAX  = float(os.getenv("DELAY_MAX", "4.0"))
MAX_ERRORS = 10

# ── Proxy ─────────────────────────────────────────────────────────────────────
def get_proxies():
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if not proxy_url:
        return None
    # Log proxy without credentials
    safe = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
    log.info(f"Using proxy: {safe}")
    return {"http": proxy_url, "https": proxy_url}


# ── Scraper factory ───────────────────────────────────────────────────────────
def make_scraper(proxies=None):
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    for k, v in STATIC_COOKIES.items():
        scraper.cookies.set(k, v, domain="ww1.m4uhd.page")
    if proxies:
        scraper.proxies.update(proxies)
    return scraper


# ── Debug: dump HTML structure ────────────────────────────────────────────────
def dump_html_structure(scraper):
    """
    Fetch page 1, print every tag+class combo and sample <a> hrefs.
    Run once locally to find the real selectors, then update CARD_SELECTORS below.
    Also saves page_dump.html so you can open it in a browser.
    """
    url = BASE_URL + "/new-movies"
    log.info(f"Fetching {url} for debug...")
    resp = scraper.get(url, headers=HEADERS, timeout=30)
    log.info(f"Status: {resp.status_code}  Length: {len(resp.text)}")

    if resp.status_code != 200:
        log.error(f"Got {resp.status_code} — cannot inspect structure")
        sys.exit(1)

    soup = BeautifulSoup(resp.text, "lxml")

    print("\n" + "="*60)
    print("ALL TAG CLASS NAMES (div / article / li / ul / section):")
    print("="*60)
    seen = set()
    for tag in soup.find_all(["div", "article", "li", "ul", "section"], class_=True):
        key = f"{tag.name}  →  {' '.join(tag.get('class', []))}"
        if key not in seen:
            seen.add(key)
            print(f"  {key}")

    print("\n" + "="*60)
    print("SAMPLE <a> HREFS that look like content links (first 20):")
    print("="*60)
    count = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(k in href for k in ["/watch", "/movie", "/film", "/tv", "/series", "/episode"]):
            img = a.find("img")
            print(f"  href : {href}")
            print(f"  title: {a.get('title', '')}")
            print(f"  text : {a.get_text(strip=True)[:60]}")
            print(f"  img  : {img.get('data-src') or img.get('src','') if img else 'none'}")
            print()
            count += 1
            if count >= 20:
                break

    Path("page_dump.html").write_text(resp.text, encoding="utf-8")
    print("✓ Full HTML saved to page_dump.html")
    print("  Open it in your browser and inspect the card elements.")
    print("  Then update CARD_SELECTORS in parse_items() and re-run.\n")
    sys.exit(0)


# ── HTML Parsing ──────────────────────────────────────────────────────────────
def parse_items(html, page, section):
    soup = BeautifulSoup(html, "lxml")

    # ─────────────────────────────────────────────────────────────────────────
    # CARD_SELECTORS — tried in order, first match wins.
    #
    # If you get 0 items:
    #   1. Run:  python scraper.py --dump-html
    #   2. Look at the "ALL TAG CLASS NAMES" output
    #   3. Find the repeating pattern that wraps each movie card
    #   4. Add it at the TOP of this list and re-run
    # ─────────────────────────────────────────────────────────────────────────
    CARD_SELECTORS = [
        # ← PASTE THE REAL SELECTOR HERE AFTER RUNNING --dump-html
        # e.g. "div.flw-item", "div.TPostMv", "article.post", etc.

        # Common patterns across movie sites built on similar stacks:
        "div.flw-item",
        "div.film-detail",
        "div.TPostMv",
        "article.TPost",
        "div.item",
        "article.item",
        "div.movie-item",
        "div.film-item",
        "div.card.item",
        "li.col-xl-2",
        "li.col-lg-3",
        "div.col-xl-2.col-lg-3",
        "div[class*='item']",
        "div[class*='movie']",
        "div[class*='film']",
        "article",
    ]

    cards = []
    matched = None
    for sel in CARD_SELECTORS:
        found = soup.select(sel)
        if found:
            cards = found
            matched = sel
            log.info(f"    Selector: '{sel}' → {len(cards)} cards")
            break

    # Last-resort fallback: <a> tags that wrap an <img> and link to content
    if not cards:
        log.warning("    No card selector matched — trying <a>+<img> fallback")
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if a.find("img") and any(
                k in href for k in ["/watch", "/movie", "/film", "/tv", "/series", "/episode"]
            ):
                cards.append(a)
        if cards:
            matched = "fallback:<a>+<img>"
            log.info(f"    Fallback found {len(cards)} items")

    if not cards:
        log.warning(
            f"    Page {page}: 0 items. "
            "Run  python scraper.py --dump-html  to find the real selectors."
        )
        return []

    items = []
    for card in cards:
        item = {
            "page": page,
            "type": section,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

        # ── URL & Title ───────────────────────────────────────────────────────
        if card.name == "a":
            a_el = card
        else:
            a_el = (
                card.select_one("a[href][title]")
                or card.select_one("h2 a")
                or card.select_one("h3 a")
                or card.select_one("a[href]")
            )

        if not a_el:
            continue
        href = a_el.get("href", "")
        if not href:
            continue

        item["url"] = href if href.startswith("http") else BASE_URL + href

        # Title: title attr > heading text > link text
        item["title"] = (a_el.get("title") or "").strip()
        if not item["title"]:
            for sel in [
                ".film-name", ".movie-name", ".title",
                "h2", "h3", "h4", "strong", "span.name",
            ]:
                el = card.select_one(sel) if hasattr(card, "select_one") else None
                if el:
                    t = el.get_text(strip=True)
                    if t:
                        item["title"] = t
                        break
        if not item["title"]:
            item["title"] = a_el.get_text(strip=True)

        # ── Poster ────────────────────────────────────────────────────────────
        img = card.select_one("img[data-src]") or card.select_one("img[src]")
        if img:
            item["poster"] = (img.get("data-src") or img.get("src", "")).strip()

        # ── Badges: year / quality / duration ─────────────────────────────────
        badge_sel = (
            ".fdi-item, .badge, .quality, .year, .duration, "
            "[class*='quality'], [class*='year'], [class*='badge'], "
            "span.genre, span.tag"
        )
        for badge in card.select(badge_sel):
            t = badge.get_text(strip=True)
            if t.isdigit() and len(t) == 4:
                item.setdefault("year", t)
            elif t.upper() in {
                "HD", "CAM", "SD", "4K", "HDTV", "BLURAY",
                "1080P", "720P", "480P", "WEBDL", "WEBRIP", "DVDRIP",
            }:
                item.setdefault("quality", t)
            elif any(x in t.lower() for x in ["min", " ep", "eps"]):
                item.setdefault("duration", t)

        # ── Rating ────────────────────────────────────────────────────────────
        for sel in [
            ".film-rating span", ".rating", ".score", ".imdb",
            "[class*='rating']", "[class*='score']",
        ]:
            r_el = card.select_one(sel) if hasattr(card, "select_one") else None
            if r_el:
                item["rating"] = r_el.get_text(strip=True)
                break

        if item.get("title") and item.get("url"):
            items.append(item)

    return items


def detect_last_page(html):
    soup = BeautifulSoup(html, "lxml")
    nums = []
    for a in soup.select(
        "ul.pagination a, .pagination a, nav a, [class*='page'] a, [class*='paging'] a"
    ):
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


# ── I/O helpers ───────────────────────────────────────────────────────────────
def page_url(path, page):
    return BASE_URL + path if page == 1 else f"{BASE_URL}{path}?page={page}"


def load_existing(path):
    p = Path(path)
    if not p.exists():
        return [], 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items", [])
        max_page = max((i.get("page", 0) for i in items), default=0)
        log.info(f"  Loaded {len(items)} existing items (max page: {max_page})")
        return items, max_page
    except Exception as e:
        log.warning(f"  Could not load {path}: {e} — starting fresh")
        return [], 0


def save(path, items, section):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    out = {
        "section": section,
        "total_items": len(items),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    Path(path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Core scrape loop ──────────────────────────────────────────────────────────
def scrape_section(scraper, section, start_page, end_page, resume=True):
    cfg = ENDPOINTS[section]
    path, label, output = cfg["path"], cfg["label"], cfg["output"]

    existing_items, max_scraped = load_existing(output) if resume else ([], 0)
    actual_start = max(start_page, max_scraped + 1) if (resume and max_scraped) else start_page
    actual_end   = end_page or cfg["last_page"]

    if actual_start > actual_end:
        log.info(f"  {label}: already complete up to page {max_scraped}. Nothing to do.")
        return existing_items

    log.info(f"\n{'='*60}")
    log.info(f"  {label}")
    log.info(f"  Pages {actual_start} → {actual_end}  ({BASE_URL + path}?page=N)")
    log.info(f"  Resume={resume}  Output={output}")
    log.info(f"{'='*60}")

    all_items        = list(existing_items)
    referrer         = BASE_URL + path
    consecutive_errs = 0

    for page in range(actual_start, actual_end + 1):
        url     = page_url(path, page)
        headers = {**HEADERS, "Referer": referrer}

        log.info(f"  [{section.upper()}] Page {page}/{actual_end}  {url}")

        try:
            resp = scraper.get(url, headers=headers, timeout=30)
        except Exception as e:
            log.warning(f"    ✗ Request error: {e}")
            consecutive_errs += 1
            if consecutive_errs >= MAX_ERRORS:
                log.error(f"    {MAX_ERRORS} consecutive errors — aborting")
                break
            time.sleep(10)
            continue

        if resp.status_code == 429:
            log.warning("    ⚠ Rate limited (429) — waiting 60s")
            time.sleep(60)
            continue

        elif resp.status_code == 403:
            log.error(
                "    ✗ 403 Forbidden — Cloudflare block.\n"
                "      On GitHub Actions you need a residential proxy.\n"
                "      Set PROXY_URL secret:  http://user:pass@host:port\n"
                "      Saving progress and exiting."
            )
            save(output, all_items, section)
            sys.exit(1)

        elif resp.status_code != 200:
            log.warning(f"    ✗ HTTP {resp.status_code} — skipping")
            consecutive_errs += 1
            if consecutive_errs >= MAX_ERRORS:
                log.error(f"    {MAX_ERRORS} consecutive errors — aborting")
                break
            referrer = url
            time.sleep(3)
            continue

        consecutive_errs = 0

        # Detect real last page from first-page pagination
        if page == actual_start:
            detected = detect_last_page(resp.text)
            if detected and detected != actual_end:
                log.info(f"    ℹ Pagination says last page is {detected} (was {actual_end})")
                actual_end = detected

        items = parse_items(resp.text, page, section)
        log.info(f"    ✓ {len(items)} items")

        if not items:
            log.warning(
                "    0 items — selectors may not match the site's current HTML.\n"
                "    Run:  python scraper.py --dump-html  to inspect."
            )

        all_items.extend(items)
        save(output, all_items, section)   # persist after every page

        referrer = url
        if page < actual_end:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    log.info(f"\n  ✓ {label} done — {len(all_items)} total items\n")
    return all_items


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="M4UHD scraper")
    parser.add_argument("--type",      choices=["movies", "series", "both"], default=None)
    parser.add_argument("--start",     type=int, default=None, help="Start page")
    parser.add_argument("--end",       type=int, default=None, help="End page")
    parser.add_argument("--no-resume", action="store_true",   help="Start fresh, ignore existing data")
    parser.add_argument("--dump-html", action="store_true",
                        help="Print all HTML class names and exit (use to find card selectors)")
    args = parser.parse_args()

    proxies = get_proxies()
    scraper = make_scraper(proxies)

    if args.dump_html:
        dump_html_structure(scraper)   # exits after printing

    # CLI args take priority over env vars
    scrape_type = args.type  or os.getenv("SCRAPE_TYPE", "both").lower()
    start_page  = args.start or int(os.getenv("START_PAGE", "1"))
    end_raw     = args.end   or int(os.getenv("END_PAGE",   "0"))
    end_page    = end_raw if end_raw > 0 else None
    resume      = (not args.no_resume) and (os.getenv("RESUME", "true").lower() != "false")

    sections = ["movies", "series"] if scrape_type == "both" else [scrape_type]

    log.info(
        f"Starting — type={scrape_type}  pages={start_page}→{end_page or 'last'}"
        f"  resume={resume}  proxy={'yes' if proxies else 'no'}"
    )

    failed = []
    for section in sections:
        try:
            scrape_section(scraper, section, start_page, end_page, resume=resume)
        except SystemExit:
            raise
        except Exception as e:
            log.error(f"Section '{section}' crashed: {e}", exc_info=True)
            failed.append(section)

    if failed:
        log.error(f"Failed sections: {failed}")
        sys.exit(1)

    log.info("All done.")


if __name__ == "__main__":
    main()
