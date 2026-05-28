#!/usr/bin/env bash
# setup.sh — one-command installer for the Commercial Yield Finder local server
# Run once: bash setup.sh
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.jashan.yield-finder-server"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_SRC="$REPO_DIR/$PLIST_NAME.plist"
PLIST_DST="$LAUNCH_AGENTS/$PLIST_NAME.plist"

echo "=== Commercial Yield Finder Setup ==="
echo "Repo: $REPO_DIR"
echo ""

# ── 1. Python deps ──────────────────────────────────────────────────────────
echo "→ Installing Python dependencies…"
pip3 install -r "$REPO_DIR/requirements.txt" --quiet
python3 -m playwright install chromium --with-deps 2>&1 | tail -3
echo "   ✓ Dependencies installed"

# ── 2. .env check ───────────────────────────────────────────────────────────
if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  echo ""
  echo "⚠️  Created .env from template."
  echo "   Open it and fill in your ANTHROPIC_API_KEY and GITHUB_TOKEN:"
  echo "   $REPO_DIR/.env"
  echo ""
fi

# ── 3. LaunchAgents ─────────────────────────────────────────────────────────
echo "→ Installing LaunchAgents…"
mkdir -p "$LAUNCH_AGENTS"
chmod +x "$REPO_DIR/start-server.sh" "$REPO_DIR/start-scraper.sh"

for PLIST_NAME in "com.jashan.yield-finder-server" "com.jashan.yield-finder-scraper"; do
  PLIST_SRC="$REPO_DIR/$PLIST_NAME.plist"
  PLIST_DST="$LAUNCH_AGENTS/$PLIST_NAME.plist"
  sed "s|REPO_PATH|$REPO_DIR|g" "$PLIST_SRC" > "$PLIST_DST"
  launchctl unload "$PLIST_DST" 2>/dev/null || true
  launchctl load "$PLIST_DST"
done

echo "   ✓ Server LaunchAgent installed — starts on every login"
echo "   ✓ Scraper LaunchAgent installed — runs daily at 7:00 AM"

# ── 4. Open dashboard ────────────────────────────────────────────────────────
sleep 1
echo ""
echo "=== Done! ==="
echo ""
echo "   Dashboard → http://localhost:8765"
echo "   Server log → $REPO_DIR/server.log"
echo ""
open "http://localhost:8765" 2>/dev/null || true
