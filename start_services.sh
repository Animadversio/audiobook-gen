#!/usr/bin/env bash
# Start audiobook dashboard + tunnel + watchdog for a given book.
# Usage: ./start_services.sh BOOK_ID [GEN_PID]
#
# Dashboard runs on :8765, tunnel exposes it publicly via localhost.run.
# Current URL is written to /tmp/audiobook_tunnel_url.txt for easy retrieval.
#
# If GEN_PID is given, watchdog is launched to monitor that generation process.

set -euo pipefail

BOOK_ID="${1:-}"
GEN_PID="${2:-}"

PYTHON="/home/binxu/miniforge3/envs/research/bin/python"
LIBRARY="/home/binxu/nanoclaw/groups/discord_voxcpm-tts-4070/audiobook-library"
IPC_DIR="/home/binxu/nanoclaw/data/ipc/discord_voxcpm-tts-4070/messages"
JID="dc:1491312252140130324"
DASHBOARD_PORT=8765
TUNNEL_LOG="/tmp/tunnel.log"
URL_FILE="/tmp/audiobook_tunnel_url.txt"
CLOUDFLARED="/home/binxu/miniforge3/envs/research/bin/cloudflared"

if [[ -z "$BOOK_ID" ]]; then
    echo "Usage: $0 BOOK_ID [GEN_PID]"
    exit 1
fi

# ── Dashboard ──────────────────────────────────────────────────────────────
DASH_PID=""
if pgrep -f "audiobook_gen.py dashboard.*$BOOK_ID" > /dev/null 2>&1; then
    DASH_PID=$(pgrep -f "audiobook_gen.py dashboard.*$BOOK_ID" | head -1)
    echo "[services] Dashboard already running (PID $DASH_PID)"
else
    PYTHONIOENCODING=utf-8 nohup "$PYTHON" \
        "$(dirname "$0")/audiobook_gen.py" dashboard "$BOOK_ID" \
        --library "$LIBRARY" \
        --port "$DASHBOARD_PORT" \
        > /tmp/audiobook_dashboard.log 2>&1 &
    DASH_PID=$!
    echo "[services] Dashboard started (PID $DASH_PID) on :$DASHBOARD_PORT"
    sleep 1
fi

# ── Tunnel (cloudflared — no TTY needed, more reliable than localhost.run) ──
# Kill any stale tunnel
pkill -f "cloudflared tunnel" 2>/dev/null || true
pkill -f "nokey@localhost.run" 2>/dev/null || true
sleep 1

rm -f "$TUNNEL_LOG" "$URL_FILE"
nohup "$CLOUDFLARED" tunnel --url "localhost:$DASHBOARD_PORT" \
    > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!
echo "[services] cloudflared tunnel started (PID $TUNNEL_PID) — waiting for URL..."

# Wait up to 20s for URL to appear
for i in $(seq 1 20); do
    URL=$(grep -oP 'https://[a-z0-9.-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1)
    if [[ -n "$URL" ]]; then
        echo "$URL" > "$URL_FILE"
        echo "[services] Dashboard URL: $URL"
        break
    fi
    sleep 1
done

if [[ ! -f "$URL_FILE" ]]; then
    echo "[services] WARNING: tunnel URL not found after 20s — check $TUNNEL_LOG"
fi

# ── Watchdog ───────────────────────────────────────────────────────────────
if [[ -n "$GEN_PID" ]]; then
    PYTHONIOENCODING=utf-8 nohup "$PYTHON" \
        "$(dirname "$0")/watchdog.py" \
        --book-id "$BOOK_ID" \
        --library "$LIBRARY" \
        --gen-pid "$GEN_PID" \
        --dash-pid "$DASH_PID" \
        --ipc "$IPC_DIR" \
        --jid "$JID" \
        --stall-secs 300 \
        > /tmp/watchdog.log 2>&1 &
    echo "[services] Watchdog started (PID $!) monitoring gen PID $GEN_PID"
fi

echo ""
echo "Dashboard: $(cat $URL_FILE 2>/dev/null || echo 'URL pending')"
echo "Tunnel log: $TUNNEL_LOG"
echo "URL file:   $URL_FILE  (cat this any time for current URL)"
echo "Watchdog:   /tmp/watchdog.log"
