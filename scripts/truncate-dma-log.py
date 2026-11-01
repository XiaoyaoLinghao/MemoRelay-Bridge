#!/usr/bin/env python3
"""Truncate DMA log to keep only recent entries (default 30 days).

Reads the DMA log, filters lines to keep only those from the last N days,
and atomically replaces the log file.  Continuation lines (without timestamps)
that follow kept timestamped lines are preserved.

Usage:
    truncate-dma-log.py                  # keep 30 days
    truncate-dma-log.py --days 14        # keep 14 days
    truncate-dma-log.py --dry-run        # print stats only, don't modify
"""

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

LOG_PATH = "/home/openclaw/.openclaw/logs/daily-memory-archiver.log"
TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})]")


def parse_args():
    p = argparse.ArgumentParser(description="Truncate DMA log by age")
    p.add_argument("--days", type=int, default=30, help="Days to keep (default: 30)")
    p.add_argument("--dry-run", action="store_true", help="Print stats only")
    return p.parse_args()


def main():
    args = parse_args()
    cutoff = datetime.now() - timedelta(days=args.days)

    if not os.path.isfile(LOG_PATH):
        print(f"Log file not found: {LOG_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(LOG_PATH, "r") as f:
        lines = f.readlines()

    total_lines = len(lines)
    total_bytes = sum(len(l) for l in lines)

    # State-machine filter: keep lines from recent blocks
    kept: list[str] = []
    in_recent_block = False
    for line in lines:
        m = TIMESTAMP_RE.match(line)
        if m:
            try:
                line_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                in_recent_block = line_ts >= cutoff
            except ValueError:
                pass
        if in_recent_block:
            kept.append(line)

    kept_lines = len(kept)
    kept_bytes = sum(len(l) for l in kept)
    removed_lines = total_lines - kept_lines
    removed_pct = (removed_lines / total_lines * 100) if total_lines else 0

    print(f"Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M:%S')} ({args.days} days ago)")
    print(f"Total:  {total_lines:>6} lines  {total_bytes:>10} bytes")
    print(f"Kept:   {kept_lines:>6} lines  {kept_bytes:>10} bytes")
    print(f"Removed:{removed_lines:>6} lines  ({removed_pct:.1f}%)")

    if args.dry_run:
        print("[dry-run] Log not modified.")
        return

    if removed_lines == 0:
        print("Nothing to remove.")
        return

    # Atomic replacement: write to temp file, then rename
    log_dir = os.path.dirname(LOG_PATH)
    fd, tmp = tempfile.mkstemp(dir=log_dir, prefix=".dma-log-trunc-")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(kept)
        os.rename(tmp, LOG_PATH)
        print(f"Log truncated successfully.")
    except Exception:
        os.unlink(tmp)
        raise


if __name__ == "__main__":
    main()
