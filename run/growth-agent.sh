#!/bin/sh
# Daily growth-agent runner — invoked by launchd (com.owera.growth-agent.plist).
#
# Runs headless Grok against this repo with the versioned playbook, fully
# autonomous but bounded by the guardrails written into the playbook itself.
#
# Kill switches (either stops the next run, no unload needed):
#   touch run/growth-agent.disabled      # hard off
#   launchctl bootout gui/$(id -u)/com.owera.growth-agent   # remove the timer
#
# Logs: ~/Library/Logs/owera-growth-agent.log

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

# launchd gives a minimal PATH; put grok, uv, node/npx, ffmpeg, git on it.
export PATH="$HOME/.grok/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# The API is guarded by HTTP Basic Auth when MANAGER_APP_PASSWORD is set (see
# app/main.py basic_auth middleware). Load it from .env and export it so both the
# health check below and the agent's own curl calls can authenticate. Empty is
# fine: the middleware disables itself, and `-u agent:` is then harmless.
MANAGER_APP_PASSWORD="$(grep -E '^MANAGER_APP_PASSWORD=' "$REPO/.env" 2>/dev/null | head -n1 | cut -d= -f2-)"
export MANAGER_APP_PASSWORD

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

NOTIFY="$REPO/run/notify-agent.sh"
alert() {
  log "ALERT: $*"
  [ -x "$NOTIFY" ] && "$NOTIFY" "growth-agent" "${2:-1}" "$1" || true
}

# --- Preconditions --------------------------------------------------------
if ! command -v grok >/dev/null 2>&1; then
  log "ERROR: 'grok' CLI not found on PATH — install Grok or fix PATH"
  alert "grok CLI not found" 1
  exit 1
fi
if ! curl -sf -o /dev/null -u "agent:$MANAGER_APP_PASSWORD" http://127.0.0.1:7070/api/dashboard; then
  log "app not reachable / auth failed on :7070 — skipping (is the manager running? is MANAGER_APP_PASSWORD correct?)"
  # Soft skip (manager down at 09:00 is recoverable), but still surface it.
  alert "manager :7070 unreachable — skipped" 0
  exit 0
fi

# --- Skip heavy grok when renders are active --------------------------------
# Stacking the daily grok digest/rubric on top of storyboard compose is a
# primary grok.Timeout source (CLI single-flight / 600s wall). Soft-skip;
# launchd will try again tomorrow (or next timer fire).
DASH_JSON="$(curl -sf -u "agent:$MANAGER_APP_PASSWORD" http://127.0.0.1:7070/api/dashboard 2>/dev/null || true)"
RENDERING="$(printf '%s' "$DASH_JSON" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(0); raise SystemExit
n = 0
for row in data if isinstance(data, list) else []:
    counts = row.get("counts") or {}
    n += int(counts.get("rendering") or 0)
print(n)
' 2>/dev/null || echo 0)"
if [ "${RENDERING:-0}" -gt 0 ]; then
  log "active renders=$RENDERING — skipping heavy grok run (avoid Timeout stack)"
  exit 0
fi

# --- Run ------------------------------------------------------------------
log "starting daily run"
# { } is not a subshell — STATUS set inside remains visible after the group.
{
  echo "================ $(ts) growth-agent run ================"
  grok --prompt-file "$REPO/run/daily-agent-playbook.md" \
    --permission-mode bypassPermissions \
    --cwd "$REPO"
  STATUS=$?
  # Capture STATUS before $(ts): command substitution would clobber $?.
  echo "---------------- $(ts) run complete (exit $STATUS) ----------------"
} >> "$LOG" 2>&1
log "done (exit $STATUS)"
if [ "$STATUS" -ne 0 ]; then
  alert "see ~/Library/Logs/owera-growth-agent.log" "$STATUS"
fi
exit "$STATUS"
