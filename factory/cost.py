#!/usr/bin/env python3
"""Measured model spend of the OpenCode seats, from OpenCode's local database.

Usage: scripts/cost.py [--since ISO8601|epoch_ms] [--until ...] [--title-substr ROOMID]
Prints per-session and per-model tokens and USD. Prices are Nebius Token Factory list
prices (USD / 1M tokens); cache reads are charged at the full input price (conservative),
reasoning at the output price. Also prints OpenCode's own computed cost.
"""
import argparse, datetime as dt, json, sqlite3, collections, os
PRICE = {  # in, out
    "zai-org/GLM-5.3": (1.40, 4.40), "deepseek-ai/DeepSeek-V4-Pro-0813": (1.32, 3.96),
    "deepseek-ai/DeepSeek-V4.1-Flash": (0.30, 1.20), "MiniMaxAI/MiniMax-M3": (0.30, 1.20),
    "moonshotai/Kimi-K3": (3.00, 15.00), "moonshotai/Kimi-K2.7-Code": (0.95, 4.00),
    "zai-org/GLM-5.3-Flash": (0.15, 0.50), "Qwen/Qwen3.5-397B-A17B": (0.60, 3.60)}
def ts(v):
    if v is None: return None
    if v.isdigit(): return int(v)
    return int(dt.datetime.fromisoformat(v).timestamp() * 1000)
ap = argparse.ArgumentParser(); ap.add_argument("--since"); ap.add_argument("--until")
ap.add_argument("--dir", help="only sessions whose directory starts with this")
a = ap.parse_args(); s, u = ts(a.since), ts(a.until)
db = sqlite3.connect(f"file:{os.path.expanduser('~/.local/share/opencode/opencode.db')}?mode=ro", uri=True)
titles = {r[0]: r[1] for r in db.execute("select id,title from session")}
per_s = collections.defaultdict(lambda: collections.Counter()); per_m = collections.defaultdict(collections.Counter)
for sid, tc, data in db.execute("select session_id,time_created,data from message"):
    if s and tc < s: continue
    if u and tc > u: continue
    d = json.loads(data)
    if d.get("role") != "assistant": continue
    t = d.get("tokens") or {}; m = d.get("modelID", "?")
    c = collections.Counter(inp=t.get("input", 0), out=t.get("output", 0), rsn=t.get("reasoning", 0),
                            cr=(t.get("cache") or {}).get("read", 0), calls=1)
    pi, po = PRICE.get(m, (0, 0))
    usd = ((c["inp"] + c["cr"]) * pi + (c["out"] + c["rsn"]) * po) / 1e6
    c["usd_micro"] = int(usd * 1e6); c["oc_micro"] = int((d.get("cost") or 0) * 1e6)
    per_s[sid].update(c); per_m[m].update(c)
tot = collections.Counter()
for sid, c in sorted(per_s.items(), key=lambda kv: -kv[1]["usd_micro"]):
    tot.update(c)
    print(f"{sid} {titles.get(sid,'')[:40]:40} calls={c['calls']:4} in+cache={(c['inp']+c['cr'])/1e3:9.1f}K out+rsn={(c['out']+c['rsn'])/1e3:7.1f}K ${c['usd_micro']/1e6:7.3f}")
for m, c in per_m.items():
    print(f"model {m:36} calls={c['calls']:4} in+cache={(c['inp']+c['cr'])/1e6:7.2f}M out+rsn={(c['out']+c['rsn'])/1e6:6.3f}M ${c['usd_micro']/1e6:7.2f}")
print(f"TOTAL calls={tot['calls']} usd(list, cache at full price)=${tot['usd_micro']/1e6:.2f}  opencode-computed=${tot['oc_micro']/1e6:.2f}")
