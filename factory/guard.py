#!/usr/bin/env python3
"""Runaway guard for an unattended Band room (factory safety interlock).

Every POLL seconds it measures (read-only):
  * text messages posted in the room during the last WINDOW seconds  (ping-pong detector)
  * model spend in the last 10 minutes and since BASE_TS              (cost detector, OpenCode DB)
and if any limit is exceeded it STOPS the band: removes the seats from the room (so no queued
message can wake them) and kills the `opencode acp` runtimes. It never posts in the room.

Usage: guard.py ROOM [--max-msgs 16] [--window 120] [--max-10min 5] [--cap-usd 253] [--poll 30]
       (cap is the absolute cost.py total that must not be exceeded; default = 02:30 baseline $178.26 + $75)
Log: /workspace/dark-factory/guard.log ; pid file /tmp/guard-<room8>.pid
"""
import argparse, datetime as dt, json, os, sqlite3, subprocess, sys, time
ap = argparse.ArgumentParser(); ap.add_argument("room")
ap.add_argument("--max-msgs", type=int, default=16); ap.add_argument("--window", type=int, default=120)
ap.add_argument("--max-10min", type=float, default=5.0); ap.add_argument("--cap-usd", type=float, default=253.0)
ap.add_argument("--poll", type=int, default=30)
ap.add_argument("--owner-scope", default="architect")
ap.add_argument("--seats", default="fahmi24trk/coder fahmi24trk/reviewer fahmi24trk/architect")
a = ap.parse_args()
LOG = "/workspace/dark-factory/guard.log"
DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
def log(m):
    line = time.strftime("%Y-%m-%d %H:%M:%S WIB ") + f"[{a.room[:8]}] " + m
    open(LOG, "a").write(line + "\n"); print(line, flush=True)
def spend(since_ms=None):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = "select coalesce(sum(json_extract(data,'$.cost')),0) from message where json_extract(data,'$.role')='assistant'"
    if since_ms: q += f" and time_created >= {int(since_ms)}"
    return c.execute(q).fetchone()[0] or 0.0
def recent_texts():
    r = subprocess.run(["band", "room", "messages", a.room, "--json", "--page", "1", "--type", "text"],
                       capture_output=True, text=True, timeout=60)
    msgs = json.loads(r.stdout)["messages"]
    now = dt.datetime.now(dt.timezone.utc)
    return [m for m in msgs if (now - dt.datetime.fromisoformat(m["inserted_at"].replace("Z", "+00:00"))).total_seconds() <= a.window]
def stop(reason):
    log("TRIGGER: " + reason + " -> removing seats from the room and killing runtimes")
    for s in a.seats.split():
        if s.endswith("/" + a.owner_scope): continue
        r = subprocess.run(["band", "chat", "remove", "--session", a.owner_scope, a.room, s], capture_output=True, text=True)
        log(f"remove {s}: {(r.stdout + r.stderr).strip()[:120]}")
    subprocess.run(["pkill", "-x", "opencode"])
    log("runtimes killed; guard exiting. Re-add seats manually only after diagnosing.")
    sys.exit(2)
open(f"/tmp/guard-{a.room[:8]}.pid", "w").write(str(os.getpid()))
log(f"guard start: max {a.max_msgs} texts/{a.window}s, max ${a.max_10min}/10min, cap total ${a.cap_usd} (now ${spend():.2f})")
fails = 0
while True:
    try:
        n = len(recent_texts()); fails = 0
        tot = spend(); v10 = spend(time.time() * 1000 - 600_000)
        if n > a.max_msgs: stop(f"{n} text messages in {a.window}s")
        if v10 > a.max_10min: stop(f"${v10:.2f} spent in the last 10 min")
        if tot > a.cap_usd: stop(f"total spend ${tot:.2f} > cap ${a.cap_usd}")
        if int(time.time()) % 600 < a.poll: log(f"ok: {n} texts/{a.window}s, ${v10:.2f}/10min, total ${tot:.2f}")
    except SystemExit:
        raise
    except Exception as e:
        fails += 1; log(f"poll error {fails}: {e!r}"[:200])
    time.sleep(a.poll)
