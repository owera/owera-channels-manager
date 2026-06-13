#!/usr/bin/env bash
# Dev launcher: starts the MPT engine (:8080) and the manager (:7000) with the
# Anthropic key loaded, builds the SPA if needed, and tails both logs.
# The systemd units in this dir are the preferred way to run unattended.
set -uo pipefail

REPO="$HOME/src/ai-engineering-youtube-channel"
export PATH="$HOME/.local/bin:$PATH"
export ANTHROPIC_API_KEY="$(grep '^ANTHROPIC_API_KEY=' "$REPO/channel/.env" | cut -d= -f2- | tr -d "'\"")"

# Build the SPA once if not present.
if [ ! -d "$REPO/manager/frontend/dist" ]; then
  echo "building frontend…"
  (cd "$REPO/manager/frontend" && npm install && npm run build)
fi

echo "starting MoneyPrinterTurbo on :8080 …"
(cd "$REPO/MoneyPrinterTurbo" && uv run uvicorn app.asgi:app --host 127.0.0.1 --port 8080 \
  > /tmp/mpt.log 2>&1 &)

echo "starting manager on :7000 (all interfaces — LAN accessible) …"
(cd "$REPO/manager" && uv run uvicorn app.main:app --host 0.0.0.0 --port 7000 \
  > /tmp/ytmanager.log 2>&1 &)

sleep 4
echo "→ open http://localhost:7000"
echo "logs: /tmp/mpt.log  /tmp/ytmanager.log"
