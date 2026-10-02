#!/usr/bin/env python3
"""Scan mandate files against the OFFICIAL track vocabulary (gate 4, mandate half).

Usage:
    python3 scripts/scan_mandates.py /abs/path/to/dark-factory-wearedevs [mandate_dir]

Uses harness/vocabulary.py from the organisers' kickoff checkout
(https://github.com/band-ai/dark-factory-wearedevs) -- the same list the organisers audit
against after close. Also checks that every mandate has a non-empty Harness: and Model:
line (gate 1). Passing this is necessary, NOT sufficient: judges also read the mandates.
"""
import pathlib
import re
import sys

if len(sys.argv) < 2:
    sys.exit(__doc__)
kickoff = pathlib.Path(sys.argv[1]).resolve()
folder = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else
                      pathlib.Path(__file__).resolve().parent.parent / "seats")
sys.path.insert(0, str(kickoff))
from harness import vocabulary  # noqa: E402

problems = 0
files = sorted(folder.glob("*.md"))
if len(files) < 3:
    print(f"gate 1: only {len(files)} mandate file(s) in {folder}; need 3+")
    problems += 1
for path in files:
    text = path.read_text(errors="replace")
    for field in ("Harness", "Model"):
        if not re.search(rf"(?im)^[-*_ \t]*{field}[*_ \t]*:[*_ \t]*[^*_\s]", text):
            print(f"gate 1: {path.name} has no `{field}:` line")
            problems += 1
    if "REPLACE_WITH_EXACT_MODEL_ID" in text:
        print(f"todo:   {path.name} still has the Model placeholder -- put the exact model id")
    for track in ("tablekeeper", "pocketful"):
        banned = set(vocabulary.for_track(track))
        for n, line in enumerate(text.splitlines(), 1):
            for kind, term in vocabulary.terms_in(line):
                if term in banned:
                    print(f"gate 4: {path.name}:{n} names {track} {kind} `{term}`")
                    problems += 1
print(f"{len(files)} mandate(s) scanned against tablekeeper + pocketful vocabulary: "
      + ("OK" if problems == 0 else f"{problems} problem(s)"))
sys.exit(1 if problems else 0)
