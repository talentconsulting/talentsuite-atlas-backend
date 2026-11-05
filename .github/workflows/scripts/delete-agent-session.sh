#!/usr/bin/env bash
# Deletes the hosted session an `azd ai agent invoke` call ran in.
#
# Usage: delete-agent-session.sh <agent-name> <file holding the invoke output>
#
# Every invoke starts a new hosted session, and an idle session counts against the subscription's
# concurrent-session quota for 30 days, so without this the quota fills and every call is refused.
# Clean-up is best effort: it warns and exits 0 so it never fails the step that called it.
# Run it from the same working directory as the invoke, so azd resolves the same agent.
set -u
agent="$1"
response_file="$2"
session_id=$(grep -oiE '(x-agent-session-id: *|"agent_session_id": *")[0-9a-f]{32,}' "$response_file" 2>/dev/null \
  | grep -oE '[0-9a-f]{32,}$' | head -1)
if [ -z "$session_id" ]; then
  echo "No hosted session id in the ${agent} response, so there is no session to delete."
  exit 0
fi
if azd ai agent sessions delete "$session_id" -n "$agent" --no-prompt >/dev/null 2>&1; then
  echo "Deleted ${agent} session ${session_id}."
else
  echo "::warning::Could not delete ${agent} session ${session_id}; it will expire 30 days after its last use."
fi
exit 0
