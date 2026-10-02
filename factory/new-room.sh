#!/usr/bin/env bash
# Create a fresh Band room owned by the Architect seat, add Coder, Reviewer and the human.
# Works around the 0.4.12 CLI decode bug ("invalid type: map, expected unit") on add:
# the add succeeds server-side, so we verify with `chat participants` instead.
# Usage: scripts/new-room.sh            -> prints the chat id
set -uo pipefail
OWNER_SCOPE=${OWNER_SCOPE:-architect}
MEMBERS=${MEMBERS:-"fahmi24trk/coder fahmi24trk/reviewer fahmi24trk"}
out=$(cd /workspace/band-work && band chat new --session "$OWNER_SCOPE" 2>&1)
id=$(grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' <<<"$out" | head -1)
[ -n "$id" ] || { echo "chat new failed: $out" >&2; exit 1; }
for m in $MEMBERS; do band chat add --session "$OWNER_SCOPE" "$id" "$m" >/dev/null 2>&1; done
sleep 1
parts=$(band chat participants --session "$OWNER_SCOPE" "$id" 2>&1)
for m in $MEMBERS; do grep -q "^$m " <<<"$parts" || { echo "missing participant $m" >&2; echo "$parts" >&2; exit 1; }; done
echo "$id"
