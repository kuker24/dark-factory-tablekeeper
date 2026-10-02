#!/usr/bin/env bash
# Create a NEW local result repository (no remote) for the submitted run.
# Usage: scripts/new-result-repo.sh /abs/path/band-work/result
# - copies README/FACTORY/LICENSE templates, seats/*.md -> mandates/, the reply gate and factory scripts
# - creates NO stage folder: all stage code is written by the band in the room.
set -euo pipefail
DEST="${1:?usage: $0 /abs/path/to/band-work/result}"
case "$DEST" in /*) ;; *) echo "use an ABSOLUTE path"; exit 1;; esac
KIT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
  echo "STOP: $DEST exists and is not empty (the submitted run needs a fresh repository)"; exit 1
fi
mkdir -p "$DEST/mandates"
cp "$KIT/result-template/README.md" "$KIT/result-template/FACTORY.md" "$KIT/result-template/LICENSE" "$DEST/"
cp "$KIT/result-template/gitignore.template" "$DEST/.gitignore"
cp "$KIT/seats/"*.md "$DEST/mandates/"
mkdir -p "$DEST/runtime" "$DEST/factory"
cp "$KIT/runtime/acp-reply-gate.py" "$DEST/runtime/"; cp -r "$KIT/runtime/seat-bin" "$DEST/runtime/"
for f in make-dispatch.sh new-room.sh new-result-repo.sh conductor.py guard.py record-room.sh secret-scan.py scan_mandates.py cost.py offline-smoke.sh; do
  cp "$KIT/scripts/$f" "$DEST/factory/"
done
mkdir -p "$(dirname "$DEST")/checks"
git -C "$DEST" init -q -b main
git -C "$DEST" config user.name "Fahmi Harun"
git -C "$DEST" config user.email "kuker24@users.noreply.github.com"
if grep -l "REPLACE_WITH_EXACT_MODEL_ID" "$DEST/mandates/"*.md >/dev/null; then
  echo "NOTE: set the 'Model:' line in $DEST/mandates/*.md to the exact model id before committing."
fi
echo "Ready: $DEST (no commit yet; commit the setup as the owner BEFORE the first dispatch)."
echo "Seat names in Band Desktop must be Architect, Coder, Reviewer (= mandate file names)."
