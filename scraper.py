#!/usr/bin/env python3
"""
Commercial Property Yield Scanner
Scrapes realcommercial.com.au + commercialrealestate.com.au daily.
Outputs listings.json for the GitHub Pages dashboard.

Criteria:
  - Price  : under $500,000 AUD
  - Yield  : 6%+ p.a. net, explicitly stated
  - Tenancy: currently leased with a real commercial tenant
  - Location: all of Australia, skipping mining towns
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from anthropic import Anthropic
from playwright.async_api import async_playwright, Page

COOKIE_FILE = os.path.join(os.path.dirname(__file__), "rc_cookies.json")
COOKIE_MAX_AGE_DAYS = 3

# ─── Config ──────────────────────────────────────────────────────────────────

MINING_TOWNS = {
    "mount isa", "mt isa", "karratha", "port hedland", "newman",
    "kalgoorlie", "broken hill", "moranbah", "tennant creek",
    "roxby downs", "tom price", "paraburdoo", "south hedland",
    "dysart", "middlemount", "blackwater", "moura", "clermont",
    "singleton", "muswellbrook",
}

# Runs visible locally so CRE's GraphQL API fires (bot-detected in headless).
# GitHub Actions sets CI=true → headless=True → falls back to HTML scraping.
HEADLESS = bool(os.environ.get("CI"))

RC_INVEST_QUERIES = [
    ("yield8", "https://www.realcommercial.com.au/invest/?includePropertiesWithin=includesurrounding&maxPrice=500000&yieldValue=8&yieldOnlyForUnpricedListings=true&activeSort=calculatedYield-desc"),
    ("yield6", "https://www.realcommercial.com.au/invest/?includePropertiesWithin=includesurrounding&maxPrice=500000&yieldValue=6&yieldOnlyForUnpricedListings=true&activeSort=calculatedYield-desc"),
]

CRE_KEYWORDS = ["yield", "return", "%25+pa"]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# ─── Helpers ─────────────────────────────────────────────────────────────────

def is_mining_town(text: str) -> bool:
    t = text.lower()
    return any(town in t for town in MINING_TOWNS)


def parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    text = text.replace(",", "").replace(" ", "")
    m = re.search(r"\$(\d+(?:\.\d+)?)(k|m)?", text, re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        val *= 1_000
    elif suffix == "m":
        val *= 1_000_000
    return int(val)


def card_to_text(card: dict) -> str:
    """Convert a CRE pagedSearchResults card to plain text for Claude."""
    parts = []
    if card.get("displayableAddress"):
        parts.append(f"Address: {card['displayableAddress']}")
    if card.get("displayablePrice"):
        parts.append(f"Price: {card['displayablePrice']}")
    if card.get("headline"):
        parts.append(f"Headline: {card['headline']}")
    highlights = [h["itemText"] for h in card.get("highlights", []) if h.get("itemText")]
    if highlights:
        parts.append(f"Highlights: {', '.join(highlights)}")
    if card.get("shortDescription"):
        parts.append(f"Description: {card['shortDescription']}")
    return "\n".join(parts)


# ─── CRE: GraphQL interception (non-headless) ────────────────────────────────

async def scrape_cre_graphql(page: Page) -> list[dict]:
    """
    Intercept CRE's own GraphQL API response — returns clean structured JSON.
    Only works when browser is non-headless (site bot-detects headless and skips the call).
    """
    captured: list[dict] = []

    async def on_response(response):
        if "gqlb" in response.url and "propertySearchQuery" in response.url:
            try:
                body = await response.json()
                results = (
                    body.get("data", {})
                        .get("searchListings", {})
                        .get("pagedSearchResults", [])
                )
                if results:
                    captured.extend(results)
                    print(f"    GraphQL → {len(results)} cards")
            except Exception:
                pass

    page.on("response", on_response)
    print("→ commercialrealestate.com.au (GraphQL intercept)")

    for kw in CRE_KEYWORDS:
        url = f"https://www.commercialrealestate.com.au/for-sale/?pr=%2C500000&os=tenanted&kw={kw}"
        print(f"    kw={kw}")
        try:
            await page.goto(url, wait_until="load", timeout=45_000)
            await asyncio.sleep(6)
        except Exception as e:
            print(f"    [error]: {e}")

    page.remove_listener("response", on_response)

    # Dedupe by adID
    seen: set = set()
    deduped = []
    for card in captured:
        aid = card.get("adID")
        if aid and aid not in seen and not is_mining_town(card_to_text(card)):
            seen.add(aid)
            deduped.append(card)

    print(f"  CRE unique cards: {len(deduped)}")
    return deduped


# ─── CRE: HTML fallback (headless / CI) ──────────────────────────────────────

async def scrape_cre_html(page: Page) -> list[dict]:
    """Fallback HTML card scraper used when GraphQL interception isn't available (CI)."""
    all_raw: list[dict] = []
    seen_urls: set[str] = set()
    print("→ commercialrealestate.com.au (HTML fallback)")

    for kw in CRE_KEYWORDS:
        url = f"https://www.commercialrealestate.com.au/for-sale/?pr=%2C500000&os=tenanted&kw={kw}"
        print(f"    kw={kw}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"    [error]: {e}")
            continue

        cards = await page.evaluate("""
            () => {
                const out = [], seen = new Set();
                document.querySelectorAll('a[href*="/property/"]').forEach(a => {
                    if (seen.has(a.href)) return;
                    seen.add(a.href);
                    const card = a.closest('article')
                              || a.closest('[class*="card"]')
                              || a.closest('[class*="listing"]')
                              || a;
                    out.push({ href: a.href, text: (card.innerText||'').trim().slice(0,600) });
                });
                return out;
            }
        """)
        for card in cards:
            href, text = card["href"], card["text"]
            if href and href not in seen_urls and not is_mining_town(text):
                seen_urls.add(href)
                all_raw.append({"url": href, "text": text,
                                 "source": "commercialrealestate.com.au"})

    if not all_raw:
        print("  ⚠  CRE HTML fallback returned 0 listings")
    else:
        print(f"  CRE unique listings: {len(all_raw)}")
    return all_raw


# ─── RC cookie management ────────────────────────────────────────────────────

def _find_active_chrome_profile(chrome_base: str) -> Optional[str]:
    """Return the Chrome profile directory that has the most recent activity."""
    best, best_ts = None, 0
    for entry in os.listdir(chrome_base):
        db = os.path.join(chrome_base, entry, "Cookies")
        if not os.path.exists(db):
            continue
        ts = os.path.getmtime(db)
        if ts > best_ts:
            best_ts = ts
            best = entry
    return best


def refresh_rc_cookies() -> list[dict]:
    """Read RC cookies directly from Chrome's local profile and save to file."""
    try:
        import browser_cookie3
        # Use the active Chrome profile (not the Default profile which may be inactive)
        chrome_base = os.path.expanduser("~/Library/Application Support/Google/Chrome")
        profile = _find_active_chrome_profile(chrome_base)
        jar = browser_cookie3.chrome(domain_name=".realcommercial.com.au", cookie_file=os.path.join(chrome_base, profile, "Cookies") if profile else None)
        cookies = []
        for c in jar:
            domain = c.domain if c.domain.startswith(".") else f".{c.domain}"
            cookies.append({
                "name": c.name,
                "value": c.value,
                "domain": domain,
                "path": c.path or "/",
                "secure": bool(c.secure),
                "httpOnly": False,
                "sameSite": "None",
            })
        if cookies:
            with open(COOKIE_FILE, "w") as f:
                json.dump({"saved_at": datetime.now().isoformat(), "cookies": cookies}, f)
            print(f"  → Refreshed {len(cookies)} RC cookies from Chrome")
        return cookies
    except ImportError:
        print("  ⚠  browser_cookie3 not installed — run: pip3 install browser_cookie3")
    except Exception as e:
        print(f"  → Could not read Chrome cookies: {e}")
    return []


def load_rc_cookies() -> list[dict]:
    """
    Load RC cookies for Playwright injection.
    Always tries Chrome first (auto-refresh), falls back to saved file.
    Warns when saved cookies exceed COOKIE_MAX_AGE_DAYS.
    """
    fresh = refresh_rc_cookies()
    if fresh:
        return fresh

    if not os.path.exists(COOKIE_FILE):
        print("  ⚠  No RC cookies — open realcommercial.com.au in Chrome at least once to enable RC scraping")
        return []

    with open(COOKIE_FILE) as f:
        data = json.load(f)

    age_days = (datetime.now() - datetime.fromisoformat(data["saved_at"])).days
    cookies = data.get("cookies", [])

    if age_days > COOKIE_MAX_AGE_DAYS:
        print(f"  ⚠  RC cookies are {age_days}d old (limit {COOKIE_MAX_AGE_DAYS}d) — open realcommercial.com.au in Chrome to refresh")
    else:
        print(f"  → Using saved RC cookies ({age_days}d old)")

    return cookies


# ─── RC: scrape via cookie injection ─────────────────────────────────────────

async def scrape_rc(page: Page) -> list[dict]:
    print("→ realcommercial.com.au (cookie injection)")
    all_raw: list[dict] = []

    if HEADLESS:
        print("  ⚠  Skipping RC in CI mode — cookies only available locally")
        return all_raw

    cookies = load_rc_cookies()
    if not cookies:
        return all_raw

    await page.context.add_cookies(cookies)

    seen_urls: set[str] = set()

    for label, base_url in RC_INVEST_QUERIES:
        current_url = base_url
        page_num = 1
        max_pages = 5
        consecutive_empty = 0

        while page_num <= max_pages:
            print(f"    {label} page {page_num}")
            try:
                await page.goto(current_url, wait_until="load", timeout=45_000)
                await asyncio.sleep(4)
            except Exception as e:
                print(f"    [error]: {e}")
                break

            title = await page.title()
            if not title:
                print("    ⚠  Blank page — cookies may be stale, open realcommercial.com.au in Chrome")
                break

            if page_num == 1:
                try:
                    # Look for result count in h1 or dedicated result-count element
                    count_text = await page.evaluate("""
                        () => {
                            const el = document.querySelector('[class*="result-count"], [class*="resultCount"], h1')
                            return el ? el.innerText : '';
                        }
                    """)
                    count_m = re.search(r"(\d+)\s*result", count_text, re.IGNORECASE)
                    if not count_m:
                        count_m = re.search(r"(\d+)", count_text)
                    total = int(count_m.group(1)) if count_m else 999
                    print(f"    → {total} results")
                    if total == 0:
                        break
                    max_pages = min(5, -(-total // 20))
                except Exception:
                    pass

            cards = await page.evaluate("""
                () => {
                    const out = [], seen = new Set();
                    document.querySelectorAll('a[href*="/for-sale/property-"]').forEach(a => {
                        if (seen.has(a.href)) return;
                        seen.add(a.href);
                        const card = a.closest('article')
                                  || a.closest('[class*="card"]')
                                  || a.closest('[class*="listing"]')
                                  || a;
                        out.push({ href: a.href, text: (card.innerText||'').trim().slice(0,600) });
                    });
                    return out;
                }
            """)

            new_this_page = 0
            for card in cards:
                href, text = card["href"], card["text"]
                if href and href not in seen_urls and not is_mining_town(text):
                    seen_urls.add(href)
                    all_raw.append({"url": href, "text": text, "source": "realcommercial.com.au"})
                    new_this_page += 1

            if new_this_page == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0

            next_el = await page.query_selector(
                'a[aria-label="Next page"], a[data-testid="next-page"], a[rel="next"]'
            )
            if next_el:
                try:
                    next_href = await next_el.get_attribute("href")
                    if not next_href:
                        break
                    current_url = next_href if next_href.startswith("http") else \
                        current_url.split("/")[0] + "//" + current_url.split("/")[2] + next_href
                    page_num += 1
                    await asyncio.sleep(1)
                    continue
                except Exception:
                    break
            else:
                current_url = re.sub(r"&pg=\d+", "", current_url) + f"&pg={page_num + 1}"
                page_num += 1

    if not all_raw:
        print("  ⚠  RC returned 0 listings")
    else:
        print(f"  RC unique listings: {len(all_raw)}")
    return all_raw


# ─── Detail page fetcher (RC + CRE HTML fallback) ────────────────────────────

async def fetch_detail(page: Page, url: str) -> str:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_selector(
                'h1, [class*="price"], [class*="Price"], [data-testid*="price"]',
                timeout=8_000,
            )
        except Exception:
            pass
        await asyncio.sleep(1)

        body_text = await page.evaluate("""
            () => {
                ['nav','footer','header','[class*="nav"]','[class*="footer"]',
                 '[class*="cookie"]','[class*="modal"]','[class*="banner"]'].forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });
                return (document.body.innerText || '').replace(/\\s+/g, ' ').trim();
            }
        """)
        return body_text[:2500]
    except Exception:
        return ""


# ─── Claude Haiku Classification ─────────────────────────────────────────────

CLASSIFY_PROMPT = """You are reviewing an Australian commercial property listing for an investor.

Hard rules — ALL must pass or qualifies is false. There is no "lean PASS":
1. Tenanted — real commercial tenant with an existing lease in place. "Seeking tenant" or vacant = FAIL.
2. Price ≤ $500,000 AUD — must be explicitly stated and at or under $500k. "Contact Agent" or missing price = FAIL.
3. Yield — must be an explicit % figure stated in the listing text. No stated % = FAIL. Below 6% p.a. = FAIL.
   Yields above 20%: almost always a business revenue/ROI figure, NOT a property cap rate — FAIL unless the text explicitly confirms it as a net property yield from rent.
4. NOT a developer guarantee (e.g. "guaranteed return for X years" on a new build = FAIL).
5. NOT in a mining town: Mt Isa, Karratha, Port Hedland, Newman, Kalgoorlie, Broken Hill,
   Moranbah, Tennant Creek, Roxby Downs, Tom Price, Dysart, Middlemount.

Listing text:
\"\"\"
{text}
\"\"\"

Respond ONLY with valid JSON, no markdown:
{{"qualifies": true or false, "yield_pct": <number or null>, "price_str": "<price or null>", "address": "<address>", "reason": "<one sentence>"}}"""


def classify_listings(raw_listings: list[dict]) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ⚠  ANTHROPIC_API_KEY not set — skipping classification")
        return []

    client = Anthropic(api_key=api_key)
    qualified: list[dict] = []
    errors = 0
    print(f"  Classifying {len(raw_listings)} listings with Claude Haiku...")

    for i, item in enumerate(raw_listings, 1):
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=300,
                messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(text=item["text"])}],
            )
            raw_json = resp.content[0].text.strip()
            raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json)
            raw_json = re.sub(r"\s*```$", "", raw_json)
            parsed = json.loads(raw_json)

            if parsed.get("qualifies"):
                qualified.append({
                    "source": item.get("source", "commercialrealestate.com.au"),
                    "url": item["url"],
                    "address": parsed.get("address", ""),
                    "price_str": parsed.get("price_str") or "",
                    "price_val": parse_price(parsed.get("price_str") or ""),
                    "yield_pct": parsed.get("yield_pct"),
                    "description": item["text"][:400],
                    "reason": parsed.get("reason", ""),
                })
        except json.JSONDecodeError:
            errors += 1
            print(f"  [JSON error #{errors}]: {resp.content[0].text[:80]}")
        except Exception as e:
            errors += 1
            print(f"  [error #{errors}]: {e}")

        if i % 20 == 0:
            time.sleep(1)

    print(f"  Qualified: {len(qualified)}/{len(raw_listings)}  (errors: {errors})")
    return qualified


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    started = datetime.now(timezone.utc)
    print(f"=== Commercial Yield Scanner {'(CI/headless)' if HEADLESS else '(local/visible)'} {started.isoformat()} ===\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-AU",
            timezone_id="Australia/Sydney",
            extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await ctx.new_page()

        # ── RC via cookie injection ───────────────────────────────────────
        rc_raw = await scrape_rc(page)

        # ── CRE: GraphQL if non-headless, HTML fallback if headless ───────
        if not HEADLESS:
            cre_cards = await scrape_cre_graphql(page)
            cre_raw = [
                {
                    "url": f"https://www.commercialrealestate.com.au{c['seoUrl']}",
                    "text": card_to_text(c),
                    "source": "commercialrealestate.com.au",
                }
                for c in cre_cards
            ]
        else:
            cre_raw = await scrape_cre_html(page)

        # ── Merge + dedupe ────────────────────────────────────────────────
        seen: set[str] = set()
        all_raw = []
        for item in rc_raw + cre_raw:
            if item["url"] not in seen:
                seen.add(item["url"])
                all_raw.append(item)

        # ── Fetch detail pages for all listings ───────────────────────────
        # Card text alone doesn't have explicit yield % — need full page body
        needs_detail = all_raw[:60]

        if needs_detail:
            print(f"\n→ Fetching {len(needs_detail)} detail pages…")
            for i, item in enumerate(needs_detail, 1):
                detail = await fetch_detail(page, item["url"])
                if detail:
                    # Prepend card text so Claude gets both structured summary + full body
                    card_summary = item.get("text", "")
                    item["text"] = (card_summary + "\n\n" + detail).strip()[:2500]
                if i % 10 == 0:
                    print(f"  fetched {i}/{len(needs_detail)}")

        await browser.close()

    # ── Classify ──────────────────────────────────────────────────────────
    qualified = classify_listings(all_raw)

    if not qualified:
        print("⚠  Zero qualifying listings — preserving existing listings.json unchanged")
        return

    qualified.sort(key=lambda x: (-(x.get("yield_pct") or 0), x.get("price_val") or 999_999))

    finished = datetime.now(timezone.utc)
    output = {
        "updated": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds()),
        "count": len(qualified),
        "listings": qualified,
    }

    out_path = os.environ.get("OUTPUT_FILE", "listings.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✓ Done in {output['duration_seconds']}s — {len(qualified)} qualifying listings → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
