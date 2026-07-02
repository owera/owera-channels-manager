#!/bin/sh
# Daily supervisor for the growth agent — invoked by launchd (com.owera.run-check.plist).
#
# Runs ~1h after the 09:00 growth-agent run. Headless Claude Code reads run-check-prompt.md
# and verifies the run finished cleanly (pushed, reported, applied), finishing anything left.
# It does NOT do growth work — only verify + finish. Bounded by the prompt's guardrails.
#
# Kill switch:  touch run/run-check.disabled
# Logs:         ~/Library/Logs/owera-run-check.log

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

# launchd gives a minimal PATH; put uv, claude, node/npx, git, curl on it.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

LOG="$HOME/Library/Logs/owera-run-check.log"
LOCK="$REPO/run/.run-check.lock"
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) run-check: $*" >> "$LOG"; }

if [ -f "$REPO/run/run-check.disabled" ]; then
  log "disabled (run/run-check.disabled present) — skipping"
  exit 0
fi
if ! mkdir "$LOCK" 2>/dev/null; then
  log "previous check still holding the lock — skipping"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

if ! command -v claude >/dev/null 2>&1; then
  log "ERROR: 'claude' CLI not found on PATH"
  exit 1
fi

log "starting daily check"
{
  echo "================ $(ts) run-check ================"
  claude -p "$(cat "$REPO/run/run-check-prompt.md")" \
    --permission-mode bypassPermissions
  echo "---------------- $(ts) check complete (exit $?) ----------------"
} >> "$LOG" 2>&1
log "done"
