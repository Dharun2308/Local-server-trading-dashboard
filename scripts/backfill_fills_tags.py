"""One-time migration: tag pre-Phase-0.3 fill records as strategy_tag='untagged'.

Idempotent — records that already carry a tag are left alone. Run once on the
live host after deploying the tagging change (the API code also defaults
missing tags to 'untagged' at read time, so this is belt-and-braces for the
stored file).
"""
import os
import json
import sys

FILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fills.json")

try:
    with open(FILLS) as f:
        records = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    print("no fills.json — nothing to backfill")
    sys.exit(0)

changed = 0
for r in records:
    if "strategy_tag" not in r:
        r["strategy_tag"] = "untagged"
        changed += 1

if changed:
    tmp = FILLS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(records, f)
    os.replace(tmp, FILLS)
print(f"backfilled {changed} of {len(records)} records")
