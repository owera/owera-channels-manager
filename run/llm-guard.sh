#!/bin/sh
# LLM hard guard for the headless `grok` agents (growth agent, code agent, run check).
# Sourced by run/*.sh right before the agent starts. See docs/LLM_GUARD.md.
#
# The agents run a consumer `grok login` session (training allowed) with
# bypassPermissions, the repo as cwd and the shared Basic password, so they can
# read EVERY workspace (API username = workspace slug, or manager.db directly).
# Google Limited Use: no Google-sourced data from a non-Owera workspace may reach
# that LLM. So an agent may start only when no workspace outside the allowlist
# (MANAGER_LLM_INTERNAL_WORKSPACE_IDS, default "1" = owera) owns a channel.
# Fail closed: a missing venv, an unreadable DB or any error blocks the run.
#
# llm_guard_preflight CALL_TYPE  -> 0 allowed; non-zero blocked (event logged to $LOG)
llm_guard_preflight() {
  _ct="$1"
  _py="$REPO/.venv/bin/python"
  if [ ! -x "$_py" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') llm_blocked_external_google_data workspace_id=unknown call_type=$_ct (no .venv python: fail closed)" >> "${LOG:-/dev/stderr}"
    return 1
  fi
  _out="$(cd "$REPO" && PYTHONPATH="$REPO" "$_py" -m app.services.llm_guard agent-preflight "$_ct" 2>&1)"
  _rc=$?
  printf '%s\n' "$_out" | sed "s/^/$(date '+%Y-%m-%d %H:%M:%S') llm-guard: /" >> "${LOG:-/dev/stderr}"
  [ "$_rc" -eq 0 ]
}
