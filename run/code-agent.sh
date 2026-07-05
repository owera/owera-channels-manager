#!/bin/sh
# Autonomous code-agent runner — invoked by launchd (com.owera.code-agent.plist).
#
# Runs headless Claude Code against this repo with run/code-agent-playbook.md. Fully
# autonomous but bounded by the guardrails in the playbook: DRAFT PRs only, never
# pushes to main, one change per cycle, gated before every PR.
#
# SHIPPED OFF: run/code-agent.disabled is committed, so this no-ops until you remove
# it. Enable with:  rm run/code-agent.disabled  (and load the plist — see the plist).
#
# Kill switches (either stops the next run, no unload needed):
#   touch run/code-agent.disabled      # hard off
#   launchctl bootout gui/$(id -u)/com.owera.code-agent   # remove the timer
#
# Logs: ~/Library/Logs/owera-code-agent.log

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

# launchd gives a minimal PATH; put uv, claude, node/npx, git, gh on it.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# The API is guarded by HTTP Basic Auth when MANAGER_APP_PASSWORD is set (app/main.py).
# Export it so the agent's /verify step can authenticate against :7070 if it needs to.
MANAGER_APP_PASSWORD="$(grep -E '^MANAGER_APP_PASSWORD=' "$REPO/.env" 2>/dev/null | head -n1 | cut -d= -f2-)"
export MANAGER_APP_PASSWORD

LOG="$HOME/Library/Logs/owera-code-agent.log"
LOCK="$REPO/run/.code-agent.lock"
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) code-agent: $*" >> "$LOG"; }

# --- Kill switch ----------------------------------------------------------
if [ -f "$REPO/run/code-agent.disabled" ]; then
  log "disabled (run/code-agent.disabled present) — skipping"
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
if ! command -v gh >/dev/null 2>&1; then
  log "ERROR: 'gh' CLI not found on PATH — needed to open draft PRs"
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  log "ERROR: gh not authenticated — run 'gh auth login'"
  exit 1
fi
# Manager on :7070 is helpful for the /verify gate but not required for every item.
if ! curl -sf -o /dev/null -u "agent:$MANAGER_APP_PASSWORD" http://127.0.0.1:7070/api/dashboard; then
  log "note: manager not reachable on :7070 — items needing a live /verify may be skipped by the agent"
fi

# --- Run ------------------------------------------------------------------
log "starting code-agent sprint"
{
  echo "================ $(ts) code-agent run ================"
  claude -p "$(cat "$REPO/run/code-agent-playbook.md")" \
    --permission-mode bypassPermissions
  echo "---------------- $(ts) run complete (exit $?) ----------------"
} >> "$LOG" 2>&1
log "done"
