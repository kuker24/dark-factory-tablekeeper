#!/usr/bin/env python3
"""acp-reply-gate: a transparent stdio proxy between Band (jamd) and an ACP coding agent
(here `opencode acp`) that stops acknowledgement ping-pong between seats.

Why: over ACP, Band posts a seat's final assistant text as a reply to whoever woke the seat,
and that reply wakes the other seat. Models asked to "say nothing" still write "Silent turn.",
"(no reply needed)", "</think>" ... so two seats can acknowledge each other forever.

What it does (and nothing else):
- Every JSON-RPC line is forwarded unchanged, in order, except `agent_message_chunk` updates,
  which are held back until the end of the turn (the response to `session/prompt`). Band
  concatenates all text of a turn into one reply, so narration between tool calls counts too.
- At the end of a turn, the turn's text is delivered only if the turn did real work
  (at least one tool call) and the text is a protocol message of some length (a line starting
  with one of the message kinds on its first line, optionally after @handles) after its last tool call. Otherwise,
  including the explicit token NO_REPLY, empty text and leaked reasoning tags, is dropped, so the
  turn completes silently and Band marks the inbound message processed without a reply.
- Only the text after the turn's last tool call is ever delivered (narration between tool calls is not).
- A turn that ends with prose that is neither a protocol message nor NO_REPLY (typically "Acknowledged,
  starting now ..." followed by nothing) would leave the seat idle forever, because nobody will message it
  again. The gate then sends the agent a short continuation prompt inside the same Band turn (at most
  $ACP_GATE_MAX_CONTINUE times per inbound message, default 2). The client sees one longer turn; the
  continuation prompt is runtime plumbing and never appears in the room.
- Each drop and continuation is logged to $ACP_GATE_LOG (default ~/.local/state/acp-reply-gate.log).
Usage: acp-reply-gate.py <agent command...>   e.g. acp-reply-gate.py opencode acp
       (when invoked as the spawn command with only runtime args, it runs `opencode <args>`).
"""
import json, os, re, subprocess, sys, threading, time

KINDS = os.environ.get("ACP_GATE_KINDS", "WORK ORDER|HANDOFF|VERDICT|QUESTION|ANSWER|BLOCKER|REPORT")
KEEP = re.compile(r"^[\s>*#`_-]*(?:@\S+[\s,]+)*(?:%s)\b" % KINDS)   # kind on the first line
LOG = os.path.expanduser(os.environ.get("ACP_GATE_LOG", "~/.local/state/acp-reply-gate.log"))
AGENT = os.environ.get("ACP_GATE_AGENT", "opencode")

argv = sys.argv[1:]
if not argv or argv[0].startswith("-") or argv[0] in ("acp",):
    argv = [AGENT] + argv
if "acp" not in argv:                        # version checks etc.: no proxying needed
    os.execvp(argv[0], argv)

def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + f"pid={os.getpid()} " + msg + "\n")
    except OSError:
        pass

# Seat shims (runtime/seat-bin): refuse band/jam commands that would post as the signed-in human.
SEAT_BIN = os.environ.get("ACP_GATE_SEAT_BIN", os.path.join(os.path.dirname(os.path.realpath(__file__)), "seat-bin"))
env = dict(os.environ)
if os.path.isdir(SEAT_BIN):
    env["PATH"] = SEAT_BIN + os.pathsep + env.get("PATH", "")
child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0, env=env)
out_lock = threading.Lock()
prompts = {}          # request id -> sessionId
held = {}             # sessionId -> [raw lines]
tools = {}            # sessionId -> tool calls in the current turn
final_from = {}       # sessionId -> index in held[] where the text after the last tool call starts
MIN_LEN = int(os.environ.get("ACP_GATE_MIN_LEN", "120"))

def emit(lines):
    with out_lock:
        for ln in lines:
            sys.stdout.buffer.write(ln)
        sys.stdout.buffer.flush()

def text_of(lines):
    parts = []
    for ln in lines:
        try:
            c = json.loads(ln)["params"]["update"].get("content") or {}
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        except Exception:
            pass
    return "".join(parts)

MAX_CONT = int(os.environ.get("ACP_GATE_MAX_CONTINUE", "2"))
CONT_TEXT = os.environ.get("ACP_GATE_CONTINUE_TEXT",
    "[runtime notice from the seat harness, not a room message] Your turn ended with prose instead of an "
    "action, and that prose was not delivered to anyone: no seat will wake you again for it. If the message "
    "that woke you asks you to do work, do that work now and finish it in this turn: run the commands, then "
    "send your protocol message with your seat send command and confirm it was sent. If nothing is required "
    "of you, end with exactly NO_REPLY.")
conts = {}            # sessionId -> continuation prompts used in the current inbound turn
pending = {}          # sessionId -> the client's original session/prompt response line, held during continuation
cont_seq = [0]

def client_to_agent():
    for ln in iter(sys.stdin.buffer.readline, b""):
        try:
            m = json.loads(ln)
            if m.get("method") == "session/prompt" and "id" in m:
                sid = (m.get("params") or {}).get("sessionId")
                prompts[json.dumps(m["id"])] = sid
                conts[sid] = 0
        except Exception:
            pass
        try:
            with out_lock_child:
                child.stdin.write(ln); child.stdin.flush()
        except BrokenPipeError:
            break
    try:
        child.stdin.close()
    except Exception:
        pass

out_lock_child = threading.Lock()

def continue_turn(sid):
    """Ask the agent to continue inside the same Band turn (the client never sees this prompt)."""
    cont_seq[0] += 1; rid = f"gate-continue-{os.getpid()}-{cont_seq[0]}"
    prompts[json.dumps(rid)] = sid; conts[sid] = conts.get(sid, 0) + 1
    req = {"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
           "params": {"sessionId": sid, "prompt": [{"type": "text", "text": CONT_TEXT}]}}
    with out_lock_child:
        child.stdin.write((json.dumps(req) + "\n").encode()); child.stdin.flush()

def agent_to_client():
    for ln in iter(child.stdout.readline, b""):
        try:
            m = json.loads(ln)
        except Exception:
            emit([ln]); continue
        if m.get("method") == "session/update":
            p = m.get("params") or {}
            sid = p.get("sessionId"); kind = (p.get("update") or {}).get("sessionUpdate")
            if kind == "agent_message_chunk":
                held.setdefault(sid, []).append(ln); continue
            if kind == "tool_call":
                tools[sid] = tools.get(sid, 0) + 1
                final_from[sid] = len(held.get(sid, []))   # text after this point is the final text
            emit([ln]); continue
        if "id" in m and "method" not in m and json.dumps(m["id"]) in prompts:
            sid = prompts.pop(json.dumps(m["id"]))
            ours = str(m["id"]).startswith("gate-continue-")
            lines = held.pop(sid, [])
            final = lines[final_from.pop(sid, 0):]
            txt = re.sub(r"(?s)<think>.*?</think>", "", text_of(final)).strip()
            ntools = tools.pop(sid, 0)
            if not ours:
                pending[sid] = ln
            if final and ntools and len(txt) >= MIN_LEN and KEEP.search(txt):
                emit(final)                    # only the text after the last tool call, never the narration
            elif lines:
                log(f"session={sid} tools={ntools} dropped final text ({len(txt)} chars): {txt[:160]!r}")
                if txt and txt.strip("`*_ .") != "NO_REPLY" and conts.get(sid, 0) < MAX_CONT and "error" not in m:
                    log(f"session={sid} continuation {conts.get(sid, 0) + 1}/{MAX_CONT}: turn ended with prose")
                    continue_turn(sid); continue
            emit([pending.pop(sid, ln)]); continue
        emit([ln])

t1 = threading.Thread(target=client_to_agent, daemon=True); t1.start()
agent_to_client()
for sid, lines in held.items():
    emit(lines)
sys.exit(child.wait())
