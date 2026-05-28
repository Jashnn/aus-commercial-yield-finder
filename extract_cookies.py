#!/usr/bin/env python3
"""
Run this on your Mac whenever RC cookies need refreshing.
Prints a JSON blob — paste it into GitHub Secret RC_COOKIES.

Usage:  python3 extract_cookies.py
"""
import json, os, sys
from datetime import datetime

try:
    import browser_cookie3
except ImportError:
    print("Run: pip3 install browser_cookie3")
    sys.exit(1)

chrome_base = os.path.expanduser("~/Library/Application Support/Google/Chrome")
best, best_ts = None, 0
for entry in os.listdir(chrome_base):
    db = os.path.join(chrome_base, entry, "Cookies")
    if os.path.exists(db):
        ts = os.path.getmtime(db)
        if ts > best_ts:
            best_ts, best = ts, entry

if not best:
    print("❌ Chrome profile not found.")
    sys.exit(1)

jar = browser_cookie3.chrome(
    domain_name=".realcommercial.com.au",
    cookie_file=os.path.join(chrome_base, best, "Cookies"),
)

cookies = []
for c in jar:
    domain = c.domain if c.domain.startswith(".") else f".{c.domain}"
    cookies.append({
        "name": c.name, "value": c.value, "domain": domain,
        "path": c.path or "/", "secure": bool(c.secure),
        "httpOnly": False, "sameSite": "None",
    })

if not cookies:
    print("❌ No RC cookies found.")
    print("   Open realcommercial.com.au in Chrome, log in, then re-run this script.")
    sys.exit(1)

output = json.dumps({"extracted_at": datetime.now().isoformat(), "cookies": cookies})

print(f"✅ {len(cookies)} RC cookies extracted from Chrome profile '{best}'")
print()
print("─" * 60)
print("Paste this entire line as GitHub Secret  RC_COOKIES:")
print("─" * 60)
print(output)
print("─" * 60)
print()
print("GitHub → repo Settings → Secrets → Actions → New secret")
print("Name: RC_COOKIES   Value: (paste above)")
print()
print(f"Reminder: re-run this script in ~30 days to refresh.")
