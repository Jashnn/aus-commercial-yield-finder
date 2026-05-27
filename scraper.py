#!/usr/bin/env python3
"""
Commercial Property Yield Scanner
Scrapes realcommercial.com.au + commercialrealestate.com.au daily.
Outputs listings.json for the GitHub Pages dashboard.

Criteria:
  - Price  : under $500,000 AUD
  - Yield  : 8%+ p.a. genuine (not developer guarantees)
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

# ─── Config ──────────────────────────────────────────────────────────────────

MINING_TOWNS = {
    "mount isa", "mt isa", "karratha", "port hedland", "newman",
    "kalgoorlie", "broken hill", "moranbah", "tennant creek",
    "roxby downs", "tom price", "paraburdoo", "south hedland",
    "dysart", "middlemount", "blackwater", "moura", "clermont",
    "singleton", "muswellbrook",  # Hunter Valley coal towns
}

RC_INVEST_URL = (
    "https://www.realcommercial.com.au/invest/"
    "?yieldValue=8&maxPrice=500000"
    "&yieldOnlyForUnpricedListings=false&activeSort=featured-asc"
)

CRE_KEYWORD_QUERIES = [
    ("return",  "https://www.commercialrealestate.com.au/for-sale/?pr=%2C500000&os=tenanted&kw=return"),
    ("yield",   "https://www.commercialrealestate.com.au/for-sale/?pr=%2C500000&os=tenanted&kw=yield"),
    ("pct_pa",  "https://www.commercialrealestate.com.au/for-sale/?pr=%2C500000&os=tenanted&kw=%25+pa"),
]

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


def extract_yield(text: str) -> Optional[float]:
    """Pull the first percentage figure from a block of text."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


# ─── Layer 1: RC /invest/ ────────────────────────────────────────────────────

async def scrape_rc_invest(page: Page) -> list[dict]:
    print("→ Layer 1: RC /invest/ (pre-filtered yield≥8%, price≤$500k)")
    await page.goto(RC_INVEST_URL, wait_until="domcontentloaded", timeout=60_000)
    await asyncio.sleep(3)  # let JS hydrate

    # Scroll to trigger lazy-loading
    for _ in range(8):
        await page.evaluate("window.scrollBy(0, 900)")
        await asyncio.sleep(0.7)
    await asyncio.sleep(2)

    raw = await page.evaluate(r"""
        () => {
            const out = [];
            const seen = new Set();
            // Cast wide net: any anchor linking to a property listing
            document.querySelectorAll(
                'a[href*="/for-sale/property-"], a[href*="/property-"]'
            ).forEach(a => {
                if (!a.href.includes('realcommercial.com.au')) return;
                if (seen.has(a.href)) return;
                seen.add(a.href);
                const card = a.closest('article')
                          || a.closest('[class*="result"]')
                          || a.closest('[class*="card"]')
                          || a.closest('[data-testid]')
                          || a;
                out.push({ href: a.href, text: (card.innerText || '').trim() });
            });
            return out;
        }
    """)

    listings = []
    for card in raw:
        href = card["href"]
        text = card["text"]
        if not href or "realcommercial.com.au" not in href:
            continue
        if is_mining_town(text):
            print(f"  [skip mining town] {href}")
            continue

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        address = next((l for l in lines if any(
            state in l for state in ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "NT", "ACT"]
        )), lines[0] if lines else "Unknown")

        price_m = re.search(r"\$[\d,]+(?:k|m)?", text, re.IGNORECASE)
        price_str = price_m.group(0) if price_m else "Contact Agent"
        price_val = parse_price(price_str)

        yield_pct = extract_yield(text)

        listings.append({
            "source": "realcommercial.com.au",
            "layer": 1,
            "url": href,
            "address": address,
            "price_str": price_str,
            "price_val": price_val,
            "yield_pct": yield_pct,
            "description": text[:500],
            "qualifies": True,
            "reason": "RC /invest/ native yield filter ≥8%, price ≤$500k",
        })

    print(f"  Found {len(listings)} RC layer-1 listings")
    return listings


# ─── Layer 2: CRE keyword search ─────────────────────────────────────────────

async def scrape_cre_keyword(page: Page, label: str, url: str) -> list[dict]:
    """Fetch all pages for one CRE keyword query. Returns raw card dicts."""
    raw = []
    seen_urls: set[str] = set()
    current_url = url
    page_num = 1

    while True:
        print(f"    kw={label} page {page_num} → {current_url}")
        try:
            await page.goto(current_url, wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(3)  # let JS hydrate
        except Exception as e:
            print(f"    [timeout/error page {page_num}]: {e}")
            break
        await asyncio.sleep(1.5)

        if page_num == 1:
            try:
                count_el = await page.wait_for_selector("h1", timeout=5_000)
                count_text = await count_el.inner_text()
                count_m = re.search(r"(\d+)", count_text)
                total = int(count_m.group(1)) if count_m else 0
                print(f"    → {total} results")
                if total == 0:
                    break
            except Exception:
                pass

        cards = await page.evaluate("""
            () => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll('a[href*="/property/"]').forEach(a => {
                    if (!a.href.includes('commercialrealestate.com.au')) return;
                    if (seen.has(a.href)) return;
                    seen.add(a.href);
                    const card = a.closest('article')
                             || a.closest('[class*="card"]')
                             || a.closest('[class*="listing"]')
                             || a;
                    out.push({
                        href: a.href,
                        text: (card.innerText || '').trim().slice(0, 700)
                    });
                });
                return out;
            }
        """)

        for card in cards:
            href = card["href"]
            text = card["text"]
            if not href or href in seen_urls:
                continue
            if is_mining_town(text):
                continue
            seen_urls.add(href)
            raw.append({"url": href, "text": text})

        next_el = await page.query_selector(
            'a[aria-label="Next page"], '
            'a[data-testid="next-page"], '
            'a[rel="next"]'
        )
        if not next_el:
            if "&pg=" in current_url:
                pg_m = re.search(r"&pg=(\d+)", current_url)
                next_pg = int(pg_m.group(1)) + 1 if pg_m else 2
                current_url = re.sub(r"&pg=\d+", f"&pg={next_pg}", current_url)
            else:
                current_url = current_url + "&pg=2"

            page_num += 1
            if page_num > 10:
                break
            continue

        try:
            next_href = await next_el.get_attribute("href")
            if not next_href:
                break
            if next_href.startswith("http"):
                current_url = next_href
            else:
                current_url = f"https://www.commercialrealestate.com.au{next_href}"
            page_num += 1
            await asyncio.sleep(1)
        except Exception:
            break

    return raw


async def scrape_cre_all(page: Page) -> list[dict]:
    """Run all keyword queries, deduplicate by URL."""
    all_raw: list[dict] = []
    seen_urls: set[str] = set()

    print("→ Layer 2: CRE keyword searches")
    for label, url in CRE_KEYWORD_QUERIES:
        items = await scrape_cre_keyword(page, label, url)
        for item in items:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_raw.append(item)

    print(f"  CRE unique listings to classify: {len(all_raw)}")
    return all_raw


# ─── Claude Haiku Classification ─────────────────────────────────────────────

CLASSIFY_PROMPT = """You are classifying an Australian commercial property listing.
Determine if it meets ALL of these criteria:

1. Yield ≥ 8% p.a. — must be a GENUINE lease yield, NOT a developer-guaranteed return
   (e.g. "7% guaranteed for 1 year" = FAIL. "8.5% net yield, existing 5yr lease" = PASS)
2. Currently tenanted — an existing commercial tenant with a real lease already in place
3. Price under $500,000 AUD (if price is "Contact Agent" or undisclosed = uncertain, lean FAIL unless yield is explicitly stated)
4. NOT in a mining town — reject if address mentions:
   Mt Isa, Karratha, Port Hedland, Newman, Kalgoorlie, Broken Hill, Moranbah,
   Tennant Creek, Roxby Downs, Tom Price, Dysart, Middlemount

Listing text:
\"\"\"
{text}
\"\"\"

Respond ONLY with valid JSON, no markdown, no explanation:
{{"qualifies": true or false, "yield_pct": <number or null>, "price_str": "<price string or null>", "address": "<full address>", "reason": "<one sentence explaining your decision>"}}"""


def classify_listings(raw_listings: list[dict]) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ⚠  ANTHROPIC_API_KEY not set — skipping Layer 2 classification")
        return []

    client = Anthropic(api_key=api_key)
    qualified: list[dict] = []
    errors = 0

    print(f"  Classifying {len(raw_listings)} listings with Claude Haiku...")

    for i, item in enumerate(raw_listings, 1):
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=250,
                messages=[{
                    "role": "user",
                    "content": CLASSIFY_PROMPT.format(text=item["text"]),
                }],
            )
            raw_json = resp.content[0].text.strip()
            raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json)
            raw_json = re.sub(r"\s*```$", "", raw_json)
            parsed = json.loads(raw_json)

            if parsed.get("qualifies"):
                qualified.append({
                    "source": "commercialrealestate.com.au",
                    "layer": 2,
                    "url": item["url"],
                    "address": parsed.get("address", ""),
                    "price_str": parsed.get("price_str") or "",
                    "price_val": parse_price(parsed.get("price_str") or ""),
                    "yield_pct": parsed.get("yield_pct"),
                    "description": item["text"][:450],
                    "qualifies": True,
                    "reason": parsed.get("reason", ""),
                })
        except json.JSONDecodeError:
            errors += 1
            print(f"  [JSON parse error #{errors}] raw: {resp.content[0].text[:100]}")
        except Exception as e:
            errors += 1
            print(f"  [classification error #{errors}]: {e}")

        # Brief pause every 20 calls to avoid rate limit bursts
        if i % 20 == 0:
            time.sleep(1)

    print(f"  Qualified: {len(qualified)}/{len(raw_listings)}  (errors: {errors})")
    return qualified


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    started = datetime.now(timezone.utc)
    print(f"=== Commercial Yield Scanner starting {started.isoformat()} ===\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-AU",
            timezone_id="Australia/Sydney",
            extra_http_headers={"Accept-Language": "en-AU,en;q=0.9"},
        )
        # Hide webdriver flag so sites don't fingerprint us as a bot
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await ctx.new_page()

        # Layer 1
        rc_listings = await scrape_rc_invest(page)

        # Layer 2
        cre_raw = await scrape_cre_all(page)
        cre_listings = classify_listings(cre_raw)

        await browser.close()

    # Merge + deduplicate by URL
    all_listings = rc_listings + cre_listings
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in all_listings:
        if item["url"] not in seen:
            seen.add(item["url"])
            deduped.append(item)

    # Sort: highest yield first, then price ascending
    deduped.sort(key=lambda x: (-(x.get("yield_pct") or 0), x.get("price_val") or 999_999))

    finished = datetime.now(timezone.utc)
    output = {
        "updated": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds()),
        "count": len(deduped),
        "listings": deduped,
    }

    out_path = os.environ.get("OUTPUT_FILE", "listings.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✓ Done in {output['duration_seconds']}s — {len(deduped)} qualifying listings → {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
