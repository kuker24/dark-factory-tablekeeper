#!/usr/bin/env bash
# Record the BAND Desktop screen (DISPLAY :3 by default) into 30-minute MP4 segments at low fps.
# Usage: scripts/record-room.sh start|stop|status [prefix]
# Output: /workspace/dark-factory/video/<prefix>-YYYYmmdd-HHMMSS-NNN.mp4 (x264, 3 fps, CRF 30)
set -uo pipefail
D=${REC_DISPLAY:-:3}; OUT=/workspace/dark-factory/video; PIDF=/tmp/record-room.pid
PREFIX=${2:-room-raw}; FPS=${REC_FPS:-3}; SEG=${REC_SEG:-1800}
case "${1:-status}" in
start)
  [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null && { echo "already recording pid $(cat $PIDF)"; exit 0; }
  mkdir -p "$OUT"; SIZE=$(DISPLAY=$D xdpyinfo | awk '/dimensions/{print $2}')
  setsid nohup ffmpeg -nostdin -loglevel warning -f x11grab -framerate "$FPS" -video_size "$SIZE" -draw_mouse 1 -i "$D" \
    -c:v libx264 -preset veryfast -crf 30 -pix_fmt yuv420p -g $((FPS*10)) \
    -f segment -segment_time "$SEG" -reset_timestamps 1 -strftime 1 \
    "$OUT/$PREFIX-%Y%m%d-%H%M%S.mp4" > /tmp/record-room.log 2>&1 < /dev/null &
  echo $! > "$PIDF"; sleep 2; kill -0 "$(cat $PIDF)" && echo "recording $D -> $OUT/$PREFIX-*.mp4 (pid $(cat $PIDF))" || { cat /tmp/record-room.log; exit 1; } ;;
stop)
  [ -f "$PIDF" ] && kill -INT "$(cat $PIDF)" 2>/dev/null; sleep 3; rm -f "$PIDF"; echo stopped
  for f in "$OUT"/*.mp4; do printf '%s %s\n' "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null)" "$f"; done ;;
status)
  [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null && echo "recording (pid $(cat $PIDF))" || echo "not recording"
  ls -la "$OUT"/*.mp4 2>/dev/null ;;
esac
