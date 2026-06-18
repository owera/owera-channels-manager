#!/bin/sh
# Daily growth-agent runner — invoked by launchd (com.owera.growth-agent.plist).
#
# Runs headless Claude Code against this repo with the versioned playbook, fully
# autonomous but bounded by the guardrails written into the playbook itself.
#
# Kill switches (either stops the next run, no unload needed):
#   touch run/growth-agent.disabled      # hard off
#   launchctl bootout gui/$(id -u)/com.owera.growth-agent   # remove the timer
#
# Logs: ~/Library/Logs/owera-growth-agent.log

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

# launchd gives a minimal PATH; put uv, claude, node/npx, ffmpeg, git on it.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

LOG="$HOME/Library/Logs/owera-growth-agent.log"
LOCK="$REPO/run/.growth-agent.lock"
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) growth-agent: $*" >> "$LOG"; }

# --- Kill switch ----------------------------------------------------------
if [ -f "$REPO/run/growth-agent.disabled" ]; then
  log "disabled (run/growth-agent.disabled present) — skipping"
  exit 0
fi

# --- Single-run lock (mkdir is atomic) ------------------------------------
if ! mkdir "$LOCK" 2>/dev/null; then
  log "previous run still holding the lock ($LOCK) — skipping"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# --- Preconditions --------------------------------------------------------
if ! command -v claude >/dev/null 2>&1; then
  log "ERROR: 'claude' CLI not found on PATH — install Claude Code or fix PATH"
  exit 1
fi
if ! curl -sf -o /dev/null http://127.0.0.1:7000/api/dashboard; then
  log "app not reachable on :7000 — skipping (is the manager running?)"
  exit 0
fi

# --- Run ------------------------------------------------------------------
log "starting daily run"
{
  echo "================ $(ts) growth-agent run ================"
  claude -p "$(cat "$REPO/run/daily-agent-playbook.md")" \
    --permission-mode bypassPermissions
  echo "---------------- $(ts) run complete (exit $?) ----------------"
} >> "$LOG" 2>&1
log "done"
