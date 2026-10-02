#!/usr/bin/env bash
# Build the per-stage dispatch task (the ONLY human input for that stage) with the
# complete stage specification pasted in.
# Usage: scripts/make-dispatch.sh <track> <stage> <abs result repo> > task.md
set -euo pipefail
TRACK=${1:?track}; N=${2:?stage}; REPO=${3:?abs result repo}
KICK=${KICKOFF:-/workspace/dark-factory/kickoff}
CHECKS=${CHECKS:-/workspace/band-work/checks}
SCRATCH=${SCRATCH:-/workspace/band-work/scratch}
LEAD=${LEAD:-@fahmi24trk/architect}; CODER=${CODER:-@fahmi24trk/coder}; REVIEWER=${REVIEWER:-@fahmi24trk/reviewer}
SPEC="$KICK/$TRACK/spec/stage-$N.md"; [ -f "$SPEC" ] || { echo "no spec $SPEC" >&2; exit 1; }
NEXT=$((N+1))
cat <<TXT
$LEAD TASK - $TRACK, stage $N (one increment). You are the lead: run it with $CODER and $REVIEWER exactly as your mandates describe. This message is the only human input for this stage. Do not ask me anything and do not wait for me; when stage $N is accepted, or cannot proceed, send me your REPORT.

Result repository (absolute path; commit only here): $REPO
Increment name: stage-$N
Folder to deliver: $REPO/stage-$N/ (service source, Dockerfile, RUN.md, the Coder's tests)
TXT
if [ "$N" -gt 1 ]; then cat <<TXT
Carry-forward: first copy the accepted $REPO/stage-$((N-1))/ to $REPO/stage-$N/ (delete any nested .git in the copy) and extend the copy. Do not modify stage-$((N-1))/. The stage-$N folder is graded against the stage 1..$N suites, so everything earlier stages required must keep working.
Earlier specifications (still binding): $(for i in $(seq 1 $((N-1))); do printf '%s ' "$KICK/$TRACK/spec/stage-$i.md"; done)
TXT
fi
cat <<TXT
Plans folder: $REPO/plans    Review folder: $REPO/reviews
Fresh run: build only from this task, its specification and the earlier stage folders of this repository. Do not read, copy or reuse any other repository, earlier run or leftover file on this machine.
Scratch folders (message files, clones, check output; never commit them): $SCRATCH/<seat name in lower case>/
Requirements: the complete stage $N specification pasted at the end of this message (identical to $SPEC).
Supplied partial checks: $KICK/$TRACK/test/ - only a part of what is graded; a smoke signal, never the definition of done. Build to the specification. Every requirement in the specification is graded, including what the supplied checks never exercise.
Official check (the Reviewer runs it against a fresh clone of the handed-off revision; use a new --out directory every run; the first run builds a runner image and takes several minutes):
  cd $KICK && .venv/bin/python -m harness run --track $TRACK --repo <fresh clone path> --stage $N --mode isolated --out $CHECKS/$TRACK-s$N-<short revision>-<run number>
Acceptance target: the harness prints "claimed stage: $N" and every stage 1..$N suite reports pass; aim for every shipped check of suites 1..$N, then re-read the specification for what the shipped checks never exercise. A "stage $NEXT: fail" line is the expected overshoot probe: do not implement anything from later stages.
The Coder may run the same command (or host mode, without --mode isolated) while iterating.
Environment: Linux box, Docker available to every seat without sudo, Python 3 and git installed. Run containers with --network none --cpus 2 --memory 2g when checking the clean-container start. Remove containers and images you created for checks when done with them.

TXT
[ "$TRACK" = toy ] && echo "Optional starting point (toy only): $KICK/scaffold, a minimal Python service that answers health and reset."
cat <<TXT

--- SPECIFICATION: $TRACK stage $N (complete text) ---
TXT
cat "$SPEC"
echo
echo "--- END OF SPECIFICATION (FINAL PART of this task) ---"
