#!/usr/bin/env python3
"""Fail if a file contains the Nebius key (or any 12+ char slice of it), or other obvious secrets.
Never prints the key. Key file: $SECRET_KEY_FILE (default: the OpenCode provider key file).
Usage: secret-scan.py FILE..."""
import sys, os, re
KEYFILE = os.path.expanduser(os.environ.get("SECRET_KEY_FILE", "~/.config/opencode/nebius.key"))
key = open(KEYFILE).read().strip()
bad = 0
for p in sys.argv[1:]:
    t = open(p, errors="replace").read()
    hits = []
    if key and key in t: hits.append("full key")
    elif key and any(key[i:i+16] in t for i in range(0, max(1, len(key)-16), 4)): hits.append("key fragment")
    for name, pat in [("github token", r"gh[pousr]_[A-Za-z0-9]{30,}"), ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY"),
                      ("bearer", r"Bearer [A-Za-z0-9._-]{30,}")]:
        if re.search(pat, t): hits.append(name)
    warn = " (note: mentions the key file name, not its content)" if os.path.basename(KEYFILE) in t else ""
    print(f"{p}: {'CLEAN' if not hits else 'FOUND: ' + ', '.join(hits)}{warn}")
    bad += bool(hits)
sys.exit(1 if bad else 0)
