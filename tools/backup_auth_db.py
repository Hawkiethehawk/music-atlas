#!/usr/bin/env python3
"""Create and verify an atomic SQLite backup for the Music Atlas identity store."""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "runtime" / "web" / "auth.sqlite")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "runtime" / "backups")
    parser.add_argument("--keep-days", type=int, default=14)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.db.resolve(strict=True)
    output = args.output.resolve()
    if args.keep_days < 1:
        raise SystemExit("--keep-days must be positive")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output / f"auth-{stamp}.sqlite"
    temporary = output / f".{target.name}.{os.getpid()}.tmp"
    sidecars = [
        Path(f"{temporary}-wal"),
        Path(f"{temporary}-shm"),
        Path(f"{target}-wal"),
        Path(f"{target}-shm"),
    ]
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src, \
             closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            dst.commit()
            dst.execute("PRAGMA journal_mode=DELETE").fetchone()
            dst.commit()
        with closing(sqlite3.connect(f"file:{temporary}?mode=ro&immutable=1", uri=True)) as check:
            result = check.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"backup quick_check failed: {result}")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        for sidecar in sidecars:
            sidecar.unlink(missing_ok=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)
    removed = 0
    for candidate in output.glob("auth-????????T??????Z.sqlite"):
        if candidate.resolve().parent != output:
            continue
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            candidate.unlink()
            removed += 1
    print(f"backup={target} bytes={target.stat().st_size} removed={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
