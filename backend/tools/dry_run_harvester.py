#!/usr/bin/env python3
"""Dry run of the token harvester — shows sums without DB insert.

Usage:
    python3 backend/tools/dry_run_harvester.py

Or with specific paths:
    python3 backend/tools/dry_run_harvester.py ~/.mc/agents/rex/claude-config/projects ~/.claude/projects

What it does:
- Reads all *.jsonl files under the given paths
- Parses all assistant lines (same logic as the real harvester)
- Prints sums: files, lines, tokens, model distribution
- No DB — no insert, no side effects
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

# Make sure app/ is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.token_harvester import parse_transcript_line, _should_attribute_boss_path


def scan_paths(base_paths: list[str]) -> None:
    total_files = 0
    total_lines = 0
    total_parsed = 0
    total_skipped_synth = 0
    total_skipped_user = 0
    total_skipped_boss_private = 0

    by_model: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0
    })

    for base_str in base_paths:
        base = Path(base_str).expanduser()
        if not base.exists():
            print(f"  [SKIP] Pfad existiert nicht: {base}")
            continue

        is_boss_path = ".claude" in str(base)
        print(f"\n  Scanne: {base} ({'boss-path' if is_boss_path else 'agent-path'})")

        for jsonl_path in sorted(base.glob("**/*.jsonl")):
            total_files += 1
            try:
                with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        total_lines += 1
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # User lines
                        if raw.get("type") == "user":
                            total_skipped_user += 1
                            continue

                        rec = parse_transcript_line(line)
                        if rec is None:
                            if raw.get("type") == "assistant":
                                model = raw.get("message", {}).get("model", "")
                                if "<synthetic>" in (model or ""):
                                    total_skipped_synth += 1
                            continue

                        # Boss attribution check
                        if is_boss_path:
                            if not _should_attribute_boss_path(
                                rec.get("cwd", ""), rec.get("git_branch")
                            ):
                                total_skipped_boss_private += 1
                                continue

                        total_parsed += 1
                        m = rec["model"]
                        by_model[m]["count"] += 1
                        by_model[m]["input"] += rec["input_tokens"]
                        by_model[m]["output"] += rec["output_tokens"]
                        by_model[m]["cache_read"] += rec["cache_read_tokens"]
                        by_model[m]["cache_write"] += rec["cache_write_tokens"]

            except OSError as e:
                print(f"    [FEHLER] {jsonl_path}: {e}")

    print("\n" + "=" * 70)
    print(f"ZUSAMMENFASSUNG")
    print("=" * 70)
    print(f"  Dateien gescannt:        {total_files:>10,}")
    print(f"  Zeilen gelesen:          {total_lines:>10,}")
    print(f"  Events geparst:          {total_parsed:>10,}")
    print(f"  Uebersprungen (user):    {total_skipped_user:>10,}")
    print(f"  Uebersprungen (synth):   {total_skipped_synth:>10,}")
    print(f"  Uebersprungen (privat):  {total_skipped_boss_private:>10,}")

    if by_model:
        print("\n  MODELL-VERTEILUNG:")
        print(f"  {'Modell':<50} {'Events':>8} {'Input':>12} {'Output':>12} {'Cache-R':>12}")
        print("  " + "-" * 98)
        for model, stats in sorted(by_model.items(), key=lambda x: -x[1]["count"]):
            short = model[:50]
            print(
                f"  {short:<50} {stats['count']:>8,} {stats['input']:>12,} "
                f"{stats['output']:>12,} {stats['cache_read']:>12,}"
            )

        total_input = sum(s["input"] for s in by_model.values())
        total_output = sum(s["output"] for s in by_model.values())
        total_cr = sum(s["cache_read"] for s in by_model.values())
        total_cw = sum(s["cache_write"] for s in by_model.values())
        print("  " + "-" * 98)
        print(
            f"  {'GESAMT':<50} {total_parsed:>8,} {total_input:>12,} "
            f"{total_output:>12,} {total_cr:>12,}"
        )
        print(f"\n  Cache-Write (gesamt): {total_cw:,}")

    print("=" * 70)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        paths = sys.argv[1:]
    else:
        # Default: all relevant paths
        paths = [
            "~/.mc/agents",
            "~/.claude/projects",
        ]

    print(f"Token Harvester — Dry Run")
    print(f"Pfade: {paths}")
    scan_paths(paths)
