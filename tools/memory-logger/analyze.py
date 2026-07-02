#!/usr/bin/env python3
"""Wachstumsrate pro Container und pro Prozess-Typ über CSV-Snapshots."""
import csv
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def main(csv_path: str):
    rows = []
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            r['ts'] = datetime.fromisoformat(r['timestamp'])
            for k in ('total_mb', 'claude_mb', 'bun_mb', 'node_mb'):
                r[k] = int(float(r.get(k, 0) or 0))
            rows.append(r)

    if not rows:
        print("Keine Daten.")
        return

    by_container = defaultdict(list)
    for r in rows:
        by_container[r['container']].append(r)

    print(f"\n{'Container':<25} {'Δh':>6} {'total Δ':>10} {'claude Δ':>10} {'bun Δ':>9} {'node Δ':>9}")
    print("-" * 75)
    for c, snaps in sorted(by_container.items()):
        snaps.sort(key=lambda x: x['ts'])
        first, last = snaps[0], snaps[-1]
        hours = (last['ts'] - first['ts']).total_seconds() / 3600
        if hours < 0.5:
            continue
        d_total = last['total_mb'] - first['total_mb']
        d_claude = last['claude_mb'] - first['claude_mb']
        d_bun = last['bun_mb'] - first['bun_mb']
        d_node = last['node_mb'] - first['node_mb']
        print(f"{c:<25} {hours:>6.1f} {d_total:>+10} {d_claude:>+10} {d_bun:>+9} {d_node:>+9}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        csv_path = sys.argv[1]
    else:
        matches = sorted(Path.home().glob("Library/Logs/openclaw-memlog/snapshots-*.csv"))
        if not matches:
            print("Keine Snapshot-CSVs gefunden.")
            sys.exit(0)
        csv_path = str(matches[-1])
    print(f"Analyse: {csv_path}")
    main(csv_path)
