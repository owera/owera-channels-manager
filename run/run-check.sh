#!/bin/sh
# Daily supervisor for the growth agent — invoked by launchd (com.owera.run-check.plist).
#
# Runs ~1h after the 09:00 growth-agent run. Headless Grok reads run-check-prompt.md
# and verifies the run finished cleanly (pushed, reported, applied), finishing anything left.
# It does NOT do growth work — only verify + finish. Bounded by the prompt's guardrails.
#
# Kill switch:  touch run/run-check.disabled
# Logs:         ~/Library/Logs/owera-run-check.log

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

# launchd gives a minimal PATH; put grok, uv, node/npx, git, curl on it.
export PATH="$HOME/.grok/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

LOG="$HOME/Library/Logs/owera-run-check.log"
LOCK="$REPO/run/.run-check.lock"
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) run-check: $*" >> "$LOG"; }

if [ -f "$REPO/run/run-check.disabled" ]; then
  log "disabled (run/run-check.disabled present) — skipping"
  exit 0
fi
# --- FREEZE guard (GO Chief of Staff 2026-09-29) — HARD lock ------------------
# No run inside the 18:00-02:00 BRT night freeze, no start at/after 16:00, watchdog
# stops the agent at 17:55, and OWERA_AGENT is exported so the local git hooks refuse
# agent commits on main / in the window and any push to main. Fail closed.
if [ ! -r "$REPO/run/agent-freeze.sh" ]; then
  log "FREEZE: run/agent-freeze.sh missing — refusing to run (fail closed)"
  exit 1
fi
. "$REPO/run/agent-freeze.sh"
if ! freeze_start_guard run-check; then
  exit 0
fi
OWERA_AGENT=run-check
export OWERA_AGENT

if ! mkdir "$LOCK" 2>/dev/null; then
  log "previous check still holding the lock — skipping"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

NOTIFY="$REPO/run/notify-agent.sh"
alert() {
  log "ALERT: $*"
  [ -x "$NOTIFY" ] && "$NOTIFY" "run-check" "${2:-1}" "$1" || true
}

if ! command -v grok >/dev/null 2>&1; then
  log "ERROR: 'grok' CLI not found on PATH"
  alert "grok CLI not found" 1
  exit 1
fi

log "starting daily check"
# { } is not a subshell — STATUS set inside remains visible after the group.
{
  echo "================ $(ts) run-check ================"
  freeze_run_with_watchdog grok --prompt-file "$REPO/run/run-check-prompt.md" \
    --permission-mode bypassPermissions \
    --cwd "$REPO"
  STATUS=$?
  # Capture STATUS before $(ts): command substitution would clobber $?.
  echo "---------------- $(ts) check complete (exit $STATUS) ----------------"
} >> "$LOG" 2>&1
log "done (exit $STATUS)"
if [ "$STATUS" -ne 0 ]; then
  alert "see ~/Library/Logs/owera-run-check.log" "$STATUS"
fi
exit "$STATUS"
