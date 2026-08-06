#!/bin/sh
# Best-effort failure alert for launchd agent wrappers.
# Usage: notify-agent.sh AGENT_NAME EXIT_CODE [DETAIL]
#
# Order: macOS banner always (if available), then Telegram via Jarvis bot
# when config/jarvis.env is present. Never fails the caller.

AGENT="${1:-agent}"
CODE="${2:-1}"
DETAIL="${3:-}"

TITLE="Owera ${AGENT} failed"
MSG="exit ${CODE}"
if [ -n "$DETAIL" ]; then
  MSG="${MSG}: ${DETAIL}"
fi

# macOS notification
if command -v osascript >/dev/null 2>&1; then
  # Escape for AppleScript double-quoted strings
  T=$(printf '%s' "$TITLE" | sed 's/"/\\"/g')
  M=$(printf '%s' "$MSG" | sed 's/"/\\"/g' | tr '\n' ' ' | cut -c1-200)
  osascript -e "display notification \"$M\" with title \"$T\"" >/dev/null 2>&1 || true
fi

# Telegram via Jarvis config (optional)
JARVIS_ENV="${HOME}/src/jarvis/config/jarvis.env"
if [ -f "$JARVIS_ENV" ]; then
  # shellcheck disable=SC1090
  set -a
  # shellcheck source=/dev/null
  . "$JARVIS_ENV" 2>/dev/null || true
  set +a
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_ALLOWED_CHAT_ID:-}" ]; then
    BODY="⚠️ ${TITLE}
${MSG}
$(date '+%Y-%m-%d %H:%M:%S')"
    curl -sS -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d chat_id="${TELEGRAM_ALLOWED_CHAT_ID}" \
      --data-urlencode text="$BODY" >/dev/null 2>&1 || true
  fi
fi

exit 0
