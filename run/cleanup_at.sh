#!/usr/bin/env bash
# One-shot: permanently delete all live videos, then remove this job from cron.
# Scheduled by the manager for a specific time; self-removes after running.
set -uo pipefail
REPO="$HOME/src/ai-engineering-youtube-channel"
export PATH="$HOME/.local/bin:$PATH"
LOG="$REPO/manager/cleanup.log"

{
  echo "===================== $(date) cleanup start ====================="
  cd "$REPO/manager" && uv run python -m app.cleanup_live
  echo "===================== $(date) cleanup end ======================="
} >> "$LOG" 2>&1

# One-shot: remove this job's own cron line so it never repeats.
( crontab -l 2>/dev/null | grep -v 'cleanup_at.sh' ) | crontab -
