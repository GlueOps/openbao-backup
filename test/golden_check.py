#!/usr/bin/env python3
"""Restore a dump into a fresh server, dump it back, and verify the result
is content-identical to the golden fixture (paths, data, custom_metadata;
timestamps and version counters legitimately differ). Used by
cross-version.sh — the restore input may be the golden itself or a dump
produced by a previous server version.

Usage: golden_check.py <golden.json> <out-dump.json> [restore-from.json]
"""
import json
import subprocess
import sys

golden_path, out_path = sys.argv[1], sys.argv[2]
restore_from = sys.argv[3] if len(sys.argv) > 3 else golden_path
TOOL = ["python3", "/app/baokv.py"]


def run(*args):
    p = subprocess.run(TOOL + list(args), capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"tool failed: {' '.join(args)}\n{p.stdout}\n{p.stderr}")


def normalized(fname):
    mounts = json.load(open(fname))["mounts"]
    return {mt: {path: [s["data"], s["custom_metadata"]]
                 for path, s in secrets.items()}
            for mt, secrets in mounts.items()}


run("restore", "-i", restore_from, "--yes")
run("dump", "-o", out_path)
g, d = normalized(golden_path), normalized(out_path)
if g == d:
    n = sum(len(v) for v in d.values())
    print(f"OK: dump is content-identical to golden ({n} secrets)")
    sys.exit(0)

print("MISMATCH between golden and dump:")
for mt in sorted(set(g) | set(d)):
    gm, dm = g.get(mt, {}), d.get(mt, {})
    for path in sorted(set(gm) | set(dm)):
        if gm.get(path) != dm.get(path):
            print(f"  {mt}/{path}:\n    golden: {gm.get(path)}\n    dump:   {dm.get(path)}")
sys.exit(1)
