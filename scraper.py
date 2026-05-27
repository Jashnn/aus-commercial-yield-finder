#!/usr/bin/env python3
"""
Commercial Property Yield Scanner
Scrapes realcommercial.com.au + commercialrealestate.com.au daily.
Outputs listings.json for the GitHub Pages dashboard.

Criteria:
  - Price  : under $500,000 AUD
  - Yield  : 6%+ p.a. (show promising listings, user filters from dashboard)
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
    "singleton", "muswellbrook",
}

# RC /invest/ is bot-detected (blank page). Use keyword search instead.
RC_KEYWORD_QUERIES = [
    ("rc_yield",  "https://www.realcommercial.com.au/for-sale/?maxPrice=500000&keywords=net+yield"),
    ("rc_pct_pa", "https://www.realcommercial.com.au/for-sale/?maxPrice=500000&keywords=%25+pa"),
]

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
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


# ─── Generic keyword scraper (works for both RC and CRE) ─────────────────────

async def scrape_keyword_listings(
    page: Page,
    queries: list[tuple[str, str]],
    site_name: str,
    href_filter: str,
) -> list[dict]:
    """
    Scrape listing URLs from keyword search pages.
    href_filter: substring that must appear in property link hrefs.
    Returns list of {url, text} dicts (card text only — caller enriches).
    """
    all_raw: list[dict] = []
    seen_urls: set[str] = set()

    print(f"→ {site_name} keyword searches")
    for label, base_url in queries:
        current_url = base_url
        page_num = 1
        consecutive_empty = 0
        max_pages = 10  # will be tightened once we know total results

        while page_num <= max_pages:
            print(f"    kw={label} page {page_num}")
            try:
                await page.goto(current_url, wait_until="domcontentloaded", timeout=60_000)
                await asyncio.sleep(3)
            except Exception as e:
                print(f"    [error page {page_num}]: {e}")
                break

            # Count results on page 1 — use to cap pagination
            max_pages = 10
            if page_num == 1:
                try:
                    h1 = await page.wait_for_selector("h1", timeout=5_000)
                    h1_text = await h1.inner_text()
                    count_m = re.search(r"(\d+)", h1_text)
                    total = int(count_m.group(1)) if count_m else 0
                    print(f"    → {total} results reported")
                    if total == 0:
                        break
                    # 20 listings per page — don't fetch more pages than needed
                    max_pages = min(10, -(-total // 20))  # ceiling division
                except Exception:
                    pass

            # Extract all listing anchors
            cards = await page.evaluate(f"""
                () => {{
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('a[href*="{href_filter}"]').forEach(a => {{
                        if (seen.has(a.href)) return;
                        seen.add(a.href);
                        const card = a.closest('article')
                                  || a.closest('[class*="card"]')
                                  || a.closest('[class*="listing"]')
                                  || a.closest('[class*="result"]')
                                  || a;
                        out.push({{
                            href: a.href,
                            text: (card.innerText || '').trim().slice(0, 600)
                        }});
                    }});
                    return out;
                }}
            """)

            new_this_page = 0
            for card in cards:
                href = card["href"]
                text = card["text"]
                if not href or href in seen_urls:
                    continue
                if is_mining_town(text):
                    continue
                seen_urls.add(href)
                all_raw.append({"url": href, "text": text})
                new_this_page += 1

            if new_this_page == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break  # two empty pages = end of results
            else:
                consecutive_empty = 0

            # Advance page
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
                # Try incrementing pg param
                if "&pg=" in current_url:
                    pg_m = re.search(r"&pg=(\d+)", current_url)
                    next_pg = int(pg_m.group(1)) + 1 if pg_m else 2
                    current_url = re.sub(r"&pg=\d+", f"&pg={next_pg}", current_url)
                elif "?pg=" in current_url:
                    pg_m = re.search(r"\?pg=(\d+)", current_url)
                    next_pg = int(pg_m.group(1)) + 1 if pg_m else 2
                    current_url = re.sub(r"\?pg=\d+", f"?pg={next_pg}", current_url)
                else:
                    current_url = current_url + "&pg=2"
                page_num += 1

    print(f"  {site_name} unique listings found: {len(all_raw)}")
    return all_raw


# ─── Listing detail fetcher ───────────────────────────────────────────────────

async def fetch_listing_detail(page: Page, url: str) -> str:
    """Visit a listing page and return its full text (max 2000 chars).
    Waits for actual content to render before reading — fixes React/Next.js blank pages.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Wait for real content — price or heading — to appear in the DOM
        try:
            await page.wait_for_selector(
                'h1, [class*="price"], [class*="Price"], '
                '[data-testid*="price"], [class*="detail"], [class*="Description"]',
                timeout=8_000,
            )
        except Exception:
            pass  # proceed anyway; at least we have the DOM

        await asyncio.sleep(1)

        # Also try to grab JSON-LD structured data (most reliable on property sites)
        json_ld_text = await page.evaluate("""
            () => {
                const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
                return scripts.map(s => s.textContent).join(' ');
            }
        """)

        body_text = await page.evaluate("""
            () => {
                ['nav','footer','header',
                 '[class*="nav"]','[class*="footer"]','[class*="cookie"]',
                 '[class*="modal"]','[class*="banner"]'].forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                });
                return (document.body.innerText || '').replace(/\\s+/g, ' ').trim();
            }
        """)

        # Combine: JSON-LD first (structured), then page text
        combined = (json_ld_text + " " + body_text).strip()
        return combined[:2500]
    except Exception:
        return ""


# ─── Claude Haiku Classification ─────────────────────────────────────────────

CLASSIFY_PROMPT = """You are reviewing an Australian commercial property listing for an investor.

Show the listing (qualifies: true) if it meets ALL of:
1. Tenanted — a real commercial tenant with an existing lease (NOT "seeking tenant", NOT vacant)
2. Price ≤ $500,000 AUD — reject if clearly over. If "Contact Agent" or no price, lean PASS.
3. Return/yield — ANY specific % figure ≥ 6% p.a. is good. If yield is implied but not stated, lean PASS and note it.
4. NOT a developer guarantee (e.g. "guaranteed return for 2 years" on a new build = FAIL)
5. NOT in a mining town: Mt Isa, Karratha, Port Hedland, Newman, Kalgoorlie, Broken Hill,
   Moranbah, Tennant Creek, Roxby Downs, Tom Price, Dysart, Middlemount

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

    # Debug: show first 2 listing texts so we know what Claude sees
    for i, item in enumerate(raw_listings[:2], 1):
        preview = item["text"][:300].replace("\n", " ")
        print(f"  [debug listing {i}]: {preview}…")

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
                    "layer": 2,
                    "url": item["url"],
                    "address": parsed.get("address", ""),
                    "price_str": parsed.get("price_str") or "",
                    "price_val": parse_price(parsed.get("price_str") or ""),
                    "yield_pct": parsed.get("yield_pct"),
                    "description": item["text"][:500],
                    "qualifies": True,
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
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = await ctx.new_page()

        # ── RC keyword search ──────────────────────────────────────────────
        rc_raw = await scrape_keyword_listings(
            page, RC_KEYWORD_QUERIES,
            site_name="realcommercial.com.au",
            href_filter="/for-sale/property-",
        )

        # ── CRE keyword search ─────────────────────────────────────────────
        cre_raw = await scrape_keyword_listings(
            page, CRE_KEYWORD_QUERIES,
            site_name="commercialrealestate.com.au",
            href_filter="/property/",
        )

        # ── Enrich all listings with detail pages ──────────────────────────
        all_raw = rc_raw + cre_raw

        # Deduplicate
        seen: set[str] = set()
        deduped_raw = []
        for item in all_raw:
            if item["url"] not in seen:
                seen.add(item["url"])
                deduped_raw.append(item)

        # Tag source
        rc_urls = {i["url"] for i in rc_raw}
        for item in deduped_raw:
            item["source"] = "realcommercial.com.au" if item["url"] in rc_urls \
                else "commercialrealestate.com.au"

        # Cap total to keep runtime ≤ 8 min
        deduped_raw = deduped_raw[:80]
        print(f"\n→ Fetching {len(deduped_raw)} listing detail pages…")

        for i, item in enumerate(deduped_raw, 1):
            detail = await fetch_listing_detail(page, item["url"])
            if detail:
                item["text"] = detail
            if i % 10 == 0:
                print(f"  fetched {i}/{len(deduped_raw)}")

        await browser.close()

    # ── Classify ──────────────────────────────────────────────────────────
    qualified = classify_listings(deduped_raw)

    # Sort: highest yield first, then price ascending
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
