# shellcheck shell=sh
# Shared FREEZE guard for the autonomous agent wrappers
# (GO Chief of Staff 2026-09-29). Sourced by run/growth-agent.sh, run/code-agent.sh
# and run/run-check.sh — POSIX sh, no bashisms.
#
# HARD locks enforced here (the playbooks carry the same rules as SOFT prompt rules):
#   * No agent run STARTS inside the night freeze [18:00, 02:00) America/Fortaleza (BRT).
#   * No agent run starts at/after FREEZE_LAST_START_HOUR (16:00): a late launchd fire
#     (e.g. after wake from sleep) must not spill into the window.
#   * A watchdog stops the agent at FREEZE_KILL_AT (17:55) if it is still running.
#   * The wrappers export OWERA_AGENT so the local git hooks (.git/hooks/pre-commit,
#     pre-merge-commit, pre-push — not versioned) refuse agent commits on main, agent
#     commits/pushes inside the window, and any push to main.
#
# Only the operator (CTO) may change this file.

FREEZE_TZ="America/Fortaleza"
FREEZE_START_HOUR=18        # window opens  18:00 local
FREEZE_END_HOUR=2           # window closes 02:00 local
FREEZE_LAST_START_HOUR=16   # no new run at/after 16:00
FREEZE_KILL_AT="17:55:00"   # watchdog deadline (same day)

freeze_now_hour() {
  TZ="$FREEZE_TZ" date +%H | sed 's/^0//'
}

# freeze_in_window [HOUR] -> 0 (true) when HOUR (default: now) is in [18:00, 02:00)
freeze_in_window() {
  _h="${1:-$(freeze_now_hour)}"
  [ "$_h" -ge "$FREEZE_START_HOUR" ] || [ "$_h" -lt "$FREEZE_END_HOUR" ]
}

# freeze_start_guard NAME [HOUR] -> 0 = may start, 1 = must skip (logs why via log())
freeze_start_guard() {
  _name="$1"
  _h="${2:-$(freeze_now_hour)}"
  if freeze_in_window "$_h"; then
    command -v log >/dev/null 2>&1 && log "FREEZE: ${_h}h is inside the 18:00-02:00 BRT night freeze — $_name not started"
    return 1
  fi
  if [ "$_h" -ge "$FREEZE_LAST_START_HOUR" ]; then
    command -v log >/dev/null 2>&1 && log "FREEZE: ${_h}h is at/after the ${FREEZE_LAST_START_HOUR}:00 last-start cutoff — $_name not started (would run into the 18:00 freeze)"
    return 1
  fi
  return 0
}

# freeze_run_with_watchdog CMD [ARGS...] -> runs CMD, TERM (then KILL) at FREEZE_KILL_AT.
# Returns CMD's exit status (143/137 when the watchdog had to stop it).
freeze_run_with_watchdog() {
  _today="$(TZ="$FREEZE_TZ" date +%Y-%m-%d)"
  _deadline="$(TZ="$FREEZE_TZ" date -j -f '%Y-%m-%d %H:%M:%S' "$_today $FREEZE_KILL_AT" +%s 2>/dev/null)"
  _now="$(date +%s)"
  if [ -z "$_deadline" ] || [ "$_deadline" -le "$_now" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') FREEZE: past the $FREEZE_KILL_AT deadline (or deadline unknown) — not starting agent"
    return 1
  fi
  "$@" &
  _agent_pid=$!
  (
    trap 'kill "$_sleep_pid" 2>/dev/null; exit 0' TERM
    sleep $((_deadline - _now)) &
    _sleep_pid=$!
    wait "$_sleep_pid"
    if kill -0 "$_agent_pid" 2>/dev/null; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') FREEZE watchdog: $FREEZE_KILL_AT reached — stopping agent pid $_agent_pid"
      pkill -TERM -P "$_agent_pid" 2>/dev/null   # best effort: direct children (tool shells)
      kill -TERM "$_agent_pid" 2>/dev/null
      sleep 60 &
      _sleep_pid=$!
      wait "$_sleep_pid"
      pkill -KILL -P "$_agent_pid" 2>/dev/null
      kill -KILL "$_agent_pid" 2>/dev/null
    fi
  ) &
  _wd_pid=$!
  wait "$_agent_pid"
  _st=$?
  kill -TERM "$_wd_pid" 2>/dev/null
  wait "$_wd_pid" 2>/dev/null
  return "$_st"
}
