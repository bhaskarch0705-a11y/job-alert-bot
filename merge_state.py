"""Merge this run's saved state (copied to a temp folder) into the files from origin/main.

Used by the workflow before every push, so two runs can never wipe out each other's
seen jobs. seen.json is a union (newest date wins per key). digest.json and health.json
take this run's copy, since this run is the one that just updated them. adzuna.json
keeps the higher call count per month so the quota tracking stays honest.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OURS = Path(sys.argv[1])


def read(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


ours = read(OURS / "seen.json") or {}
theirs = read(ROOT / "seen.json") or {}
merged = dict(theirs)
for k, v in ours.items():
    if v > merged.get(k, ""):
        merged[k] = v
(ROOT / "seen.json").write_text(json.dumps(merged, indent=0, sort_keys=True))

for name, sort in (("digest.json", False), ("health.json", True)):
    mine = read(OURS / name)
    if mine is not None:
        (ROOT / name).write_text(json.dumps(mine, indent=0, sort_keys=sort))

mine, remote = read(OURS / "adzuna.json"), read(ROOT / "adzuna.json")
if mine is not None:
    calls = dict((remote or {}).get("calls", {}))
    for month, n in mine.get("calls", {}).items():
        calls[month] = max(n, calls.get(month, 0))
    mine["calls"] = calls
    (ROOT / "adzuna.json").write_text(json.dumps(mine, indent=0))

print(f"Merged state: {len(ours)} local + {len(theirs)} remote -> {len(merged)} seen keys.")
