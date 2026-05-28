#!/bin/bash
# Called by LaunchAgent daily. Runs scraper and auto-pushes to GitHub.
cd "$(dirname "$0")"

LOG="$(dirname "$0")/scraper-cron.log"
echo "" >> "$LOG"
echo "=== Auto scrape $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

# Load env vars
source .env 2>/dev/null
export $(grep -v '^#' .env | xargs 2>/dev/null)

# Run scraper
/usr/bin/python3 scraper.py >> "$LOG" 2>&1

# Auto-push listings.json to GitHub so Pages updates
git add listings.json
git -c user.name="Jashan" -c user.email="jashn.preet@gmail.com" \
    commit -m "chore: auto scrape $(date '+%Y-%m-%d %H:%M')" >> "$LOG" 2>&1 || true
git pull --rebase origin main >> "$LOG" 2>&1
git push origin main >> "$LOG" 2>&1

echo "=== Done $(date '+%H:%M:%S') ===" >> "$LOG"
