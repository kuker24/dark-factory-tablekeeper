#!/usr/bin/env python3
"""Owner-side conductor for an unattended submitted run (stands in for the human between stages).

It does exactly what the human would do and nothing more:
  * sends the stage-N task message (scripts/make-dispatch.sh output) to the Architect - the ONLY human input,
  * waits for the Architect's REPORT addressed to the human (never nudges, never re-sends),
  * runs the official isolated check on a fresh clone of the repo HEAD (owner verification),
  * dispatches stage N+1 only if the check claims stage N and the budget allows; otherwise stops.
State survives restarts: /workspace/band-work/<name>.conductor.json. Log: /workspace/dark-factory/conductor.log
Usage: conductor.py ROOM REPO [--first 1] [--last 4] [--track tablekeeper] [--stall-min 180] [--reserve-usd 15]
"""
import argparse, datetime as dt, json, os, re, sqlite3, subprocess, sys, time
ap = argparse.ArgumentParser(); ap.add_argument("room"); ap.add_argument("repo")
ap.add_argument("--first", type=int, default=1); ap.add_argument("--last", type=int, default=4)
ap.add_argument("--track", default="tablekeeper"); ap.add_argument("--stall-min", type=int, default=180)
ap.add_argument("--cap-usd", type=float, default=253.0); ap.add_argument("--reserve-usd", type=float, default=15.0)
a = ap.parse_args()
KIT = "/workspace/dark-factory"; KICK = f"{KIT}/kickoff"; CHECKS = "/workspace/band-work/checks"
ARCH = "35f584ec-0c1e-46d0-8e2c-30f8c157dc68"; HUMAN = "f4617697-7845-4a9a-ab07-a9da45b96b8a"
STATE = f"/workspace/band-work/{os.path.basename(a.repo)}.conductor.json"; LOG = f"{KIT}/conductor.log"
DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
def log(m, runlog=False):
    line = time.strftime("%Y-%m-%d %H:%M:%S WIB ") + f"[{a.room[:8]}] " + m
    open(LOG, "a").write(line + "\n"); print(line, flush=True)
    if runlog: open(f"{KIT}/RUN_LOG.md", "a").write(f"- {time.strftime('%H:%M')} [conductor {a.room[:8]}] {m}\n")
def spend(since_ms=None):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = "select coalesce(sum(json_extract(data,'$.cost')),0) from message where json_extract(data,'$.role')='assistant'"
    if since_ms: q += f" and time_created >= {int(since_ms)}"
    return c.execute(q).fetchone()[0] or 0.0
def load():
    return json.load(open(STATE)) if os.path.exists(STATE) else {"stage": a.first, "stages": {}}
def save(s): json.dump(s, open(STATE, "w"), indent=1)
def find_report(since_ms):
    r = subprocess.run(["band", "room", "messages", a.room, "--json", "--page", "1", "--type", "text"],
                       capture_output=True, text=True, timeout=60)
    for m in json.loads(r.stdout)["messages"]:
        t = dt.datetime.fromisoformat(m["inserted_at"].replace("Z", "+00:00")).timestamp() * 1000
        if t < since_ms or m.get("sender_id") != ARCH: continue
        c = m["content"]; first = re.sub(r"^(\s*@\[\[[^\]]+\]\]\s*)+", "", c).lstrip().split("\n", 1)[0]
        if HUMAN in c and first.upper().startswith(("REPORT", "BLOCKER")):
            return m, first
    return None, None
def owner_check(n):
    head = subprocess.run(["git", "-C", a.repo, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    clone = f"/tmp/owner-clone-{os.path.basename(a.repo)}-s{n}-{head}"
    subprocess.run(["rm", "-rf", clone]); subprocess.run(["git", "clone", "-q", a.repo, clone], check=True)
    out = f"{CHECKS}/owner-{os.path.basename(a.repo)}-s{n}-{head}-{int(time.time())}"
    p = subprocess.run([f"{KICK}/.venv/bin/python", "-m", "harness", "run", "--track", a.track, "--repo", clone,
                        "--stage", str(n), "--mode", "isolated", "--out", out], cwd=KICK, capture_output=True, text=True)
    open(f"{out}.stdout.txt", "w").write(p.stdout + p.stderr)
    try: rep = json.load(open(f"{out}/report.json"))
    except Exception: rep = {}
    counts = {k: f"{v.get('passed')}/{v.get('collected')}" for k, v in (rep.get("checks") or {}).items()}
    return head, rep.get("highest_contiguous", 0), counts, out
log(f"conductor start (state {STATE}) stages {a.first}..{a.last}")
while True:
    s = load(); n = s["stage"]
    if n > a.last or s.get("done"):
        log("run finished: " + s.get("done", "all stages"), runlog=True); break
    st = s["stages"].setdefault(str(n), {})
    if "dispatched_ms" not in st:
        tot = spend()
        if tot > a.cap_usd - a.reserve_usd:
            s["done"] = f"budget: total ${tot:.2f} leaves < ${a.reserve_usd} reserve; stage {n} not dispatched"; save(s); continue
        task = subprocess.run([f"{KIT}/scripts/make-dispatch.sh", a.track, str(n), a.repo], capture_output=True, text=True, check=True).stdout
        tf = f"/workspace/band-work/scratch/owner/task-{os.path.basename(a.repo)}-s{n}.md"
        os.makedirs(os.path.dirname(tf), exist_ok=True); open(tf, "w").write(task)
        st["dispatched_ms"] = int(time.time() * 1000); st["spend_at_dispatch"] = round(spend(), 2); save(s)
        r = subprocess.run(["band", "room", "send", a.room, task, "--mention", ARCH], capture_output=True, text=True)
        st["send"] = (r.stdout + r.stderr).strip()[:200]; save(s)
        log(f"stage {n} dispatched ({len(task)} chars, task file {tf}); spend ${st['spend_at_dispatch']}: {st['send']}", runlog=True)
        continue
    if "report_ms" not in st:
        try: m, first = find_report(st["dispatched_ms"])
        except Exception as e: log(f"poll error {e!r}"[:200]); time.sleep(60); continue
        if m:
            st["report_ms"] = int(time.time() * 1000); st["report_first_line"] = first; save(s)
            log(f"stage {n} REPORT received: {first[:120]}", runlog=True)
        else:
            age = (time.time() * 1000 - st["dispatched_ms"]) / 60000
            if age > a.stall_min and not st.get("stall_logged"):
                st["stall_logged"] = True; save(s)
                log(f"stage {n}: no REPORT after {age:.0f} min (no nudge allowed; guard keeps watch)", runlog=True)
            time.sleep(60); continue
    head, hc, counts, out = owner_check(n)
    st.update(head=head, claimed=hc, counts=counts, check_dir=out,
              cost=round(spend(st["dispatched_ms"]), 2), minutes=round((st["report_ms"] - st["dispatched_ms"]) / 60000, 1))
    log(f"stage {n} owner isolated check at {head}: claimed {hc} {counts}; {st['minutes']} min, ${st['cost']}", runlog=True)
    if hc >= n: s["stage"] = n + 1
    else: s["done"] = f"stage {n} not claimed by owner check (claimed {hc}); stopping at stage {hc}"
    save(s)
