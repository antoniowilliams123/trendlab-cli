#!/bin/sh
# after_write hook: TRENDLAB_FILES is a JSON list of the files just written.
command -v ruff >/dev/null 2>&1 || exit 0
python3 -c '
import json, os, subprocess
files = [f for f in json.loads(os.environ.get("TRENDLAB_FILES") or "[]") if f.endswith(".py")]
if files:
    subprocess.run(["ruff", "format", "--quiet", *files], check=False)
'
