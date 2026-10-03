#!/usr/bin/env bash
# Owner timebox for a live run (never sends a room message).
# Usage: scripts/timebox.sh HH:MM IDLE_MIN STAGE CONDUCTOR_PID [--finish]
# Before stage STAGE's REPORT: ends the run when the deadline passes, when there is no seat activity
# (LLM spend flat and no new files under band-work/checks or the result repo) for IDLE_MIN minutes,
# or when the budget guard has tripped (guard process gone; it removes seats and kills runtimes itself).
# On REPORT: without --finish it exits and leaves the conductor alone; with --finish it waits for the
# conductor to log "run finished" (or exit), then ends the run. Ending = stop conductor, seat runtimes
# (opencode-gated children of jamd), jamd, recorder, guard.
set -uo pipefail
cd /workspace/dark-factory
DEADLINE=$1; IDLE=$2; STAGE=$3; CPID=$4; FINISH=${5:-}
log(){ echo "$(date '+%F %T') $*" >> timebox.log; }
spend(){ python3 scripts/cost.py 2>/dev/null | awk '/^TOTAL/{print $NF}' | tail -1 | sed 's/.*\$//'; }
endrun(){
  log "END RUN: $1"
  kill -TERM "$CPID" 2>/dev/null; sleep 2
  JP=$(pgrep -x jamd)
  for p in $(pgrep -f '^python3 /home/box/.local/bin/opencode-gated'); do pkill -TERM -P "$p"; kill -TERM "$p"; done
  sleep 3; [ -n "$JP" ] && kill -TERM $JP
  sleep 3; scripts/record-room.sh stop >> timebox.log 2>&1
  GP=$(pgrep -f '^python3 scripts/guard.py'); [ -n "$GP" ] && kill -TERM $GP
  log "ended: conductor, seats, jamd, recorder, guard stopped; spend \$$(spend)"
  echo "- $(date +%H:%M) timebox: run ended ($1). No message sent to the room. Conductor, seat runtimes, jamd, recorder and guard stopped; spend \$$(spend)." >> RUN_LOG.md
  touch /tmp/timebox-ended; exit 0
}
ROOM8=$(source /workspace/band-work/final-room.env; echo ${FINAL_ROOM:0:8})
T0=$(date +%s); last=$(spend); lastt=$T0; dl=$(date -d "$DEADLINE" +%s); C0=$(wc -l < conductor.log)
log "timebox start deadline=$DEADLINE idle=${IDLE}m stage=$STAGE finish=${FINISH:-no} spend=\$$last"
released=""
while true; do
  new=$(tail -n +$((C0+1)) conductor.log)
  if [ -z "$released" ] && echo "$new" | rg -q "\[$ROOM8\] stage $STAGE REPORT received"; then
    released=1; log "stage $STAGE REPORT received"
    echo "- $(date +%H:%M) timebox: stage $STAGE REPORT arrived before $DEADLINE; conductor proceeds normally." >> RUN_LOG.md
    [ -z "$FINISH" ] && exit 0
  fi
  if [ -n "$released" ]; then
    if echo "$new" | rg -q "\[$ROOM8\] stage $((STAGE+1)) dispatched"; then log "conductor dispatched stage $((STAGE+1)) (budget allowed); timebox exits"; exit 0; fi
    if echo "$new" | rg -q "\[$ROOM8\] run finished" || ! kill -0 "$CPID" 2>/dev/null; then sleep 120; endrun "conductor finished after stage $STAGE"; fi
    sleep 60; continue
  fi
  pgrep -f '^python3 scripts/guard.py' > /dev/null || endrun "budget guard stopped (see guard.log)"
  now=$(date +%s); s=$(spend)
  [ -n "$s" ] && [ "$s" != "$last" ] && { last=$s; lastt=$now; }
  fm=$(find /workspace/band-work/checks /workspace/band-work/final-result -newermt "@$lastt" -type f -print -quit 2>/dev/null)
  [ -n "$fm" ] && lastt=$now
  [ "$now" -ge "$dl" ] && endrun "deadline $DEADLINE reached without stage $STAGE REPORT"
  [ $((now-lastt)) -ge $((IDLE*60)) ] && endrun "no seat activity (spend flat at \$$last, no file changes) for ${IDLE} min"
  sleep 60
done
