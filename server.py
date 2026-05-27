#!/usr/bin/env python3
"""
Local server for Commercial Yield Finder.
Serves the dashboard and triggers scraper on demand.

Usage:  python3 server.py
Then open:  http://localhost:8765
"""

import json
import os
import subprocess
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PORT = 8765
REPO_DIR = Path(__file__).parent.resolve()

# ─── Scraper state ───────────────────────────────────────────────────────────

_lock = threading.Lock()
_state = {
    "running": False,
    "log": [],
    "last_run": None,
    "last_count": None,
}


def _run_scraper():
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["log"] = ["⏳ Starting scraper…"]

    env = os.environ.copy()
    # Read from .env if present
    env_file = REPO_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())

    try:
        env["OUTPUT_FILE"] = str(REPO_DIR / "listings.json")
        proc = subprocess.Popen(
            ["python3", str(REPO_DIR / "scraper.py")],
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        for line in proc.stdout:
            line = line.rstrip()
            print(line)
            with _lock:
                _state["log"].append(line)
        proc.wait()

        # Read result count
        listings_file = REPO_DIR / "listings.json"
        if listings_file.exists():
            data = json.loads(listings_file.read_text())
            count = data.get("count", 0)
            with _lock:
                _state["last_count"] = count
                _state["log"].append(f"✅ Done — {count} qualifying listings found")
        else:
            with _lock:
                _state["log"].append("⚠️  listings.json not created")

        # Git commit + push
        token = env.get("GITHUB_TOKEN", "")
        _git_push(token)

    except Exception as e:
        with _lock:
            _state["log"].append(f"❌ Error: {e}")
    finally:
        with _lock:
            _state["running"] = False
            _state["last_run"] = time.strftime("%Y-%m-%d %H:%M")


def _git_push(token: str):
    with _lock:
        _state["log"].append("📤 Committing & pushing to GitHub…")

    if token:
        remote = f"https://Jashnn:{token}@github.com/Jashnn/aus-commercial-yield-finder.git"
    else:
        remote = "origin"

    cmds = [
        ["git", "add", "listings.json"],
        ["git", "-c", "user.name=Jashan", "-c", "user.email=jashn.preet@gmail.com",
         "commit", "-m", f"chore: manual scrape {time.strftime('%Y-%m-%d %H:%M')}"],
        ["git", "pull", "--rebase", remote, "main"],  # sync before push
    ]
    for cmd in cmds:
        subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True)

    result = subprocess.run(
        ["git", "push", remote, "main"],
        cwd=str(REPO_DIR), capture_output=True, text=True
    )
    if result.returncode == 0:
        with _lock:
            _state["log"].append("✅ Pushed to GitHub — dashboard will update in ~30s")
    else:
        with _lock:
            _state["log"].append(f"⚠️  Push failed: {result.stderr[:120]}")


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/run":
            with _lock:
                already = _state["running"]
            if not already:
                threading.Thread(target=_run_scraper, daemon=True).start()
                body = json.dumps({"status": "started"})
            else:
                body = json.dumps({"status": "already_running"})
            self._json(200, body)

        elif self.path == "/status":
            with _lock:
                snap = dict(_state)
            self._json(200, json.dumps(snap))

        else:
            super().do_GET()

    def _json(self, code, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # suppress noisy request logs


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(REPO_DIR)
    print(f"🏢  Commercial Yield Finder  →  http://localhost:{PORT}")
    print(f"    Press Ctrl+C to stop\n")
    httpd = HTTPServer(("localhost", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
