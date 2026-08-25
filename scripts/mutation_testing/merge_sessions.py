"""Merges N per-shard cosmic-ray session databases into one, for reporting.

Each CI matrix shard runs `cosmic-ray init`/`baseline`/`exec` against a
disjoint slice of files (see plan_shards.py) and produces its own
session.sqlite. cosmic-ray's own report tools (cr-html, cr-report, cr-rate)
all read a single session, so shards are combined into one before any of
those run.

Goes through cosmic_ray.work_db.WorkDB's public API rather than copying
tables with raw SQL, so this stays correct across schema changes in whatever
cosmic-ray version is pinned. Safe because each shard's job IDs are UUIDs
minted independently by that shard's own `init` -- there is nothing for two
shards to collide on.
"""

from __future__ import annotations

import argparse
import sys

from cosmic_ray.work_db import WorkDB


def merge(shard_paths: list[str], out_path: str) -> None:
    merged = WorkDB(out_path, WorkDB.Mode.create)
    try:
        for shard_path in shard_paths:
            shard_db = WorkDB(shard_path, WorkDB.Mode.open)
            try:
                merged.add_work_items(shard_db.work_items)
                for job_id, result in shard_db.results:
                    merged.set_result(job_id, result)
            finally:
                shard_db.close()
    finally:
        merged.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Path to write the merged session to (must not exist).")
    parser.add_argument("shards", nargs="+", help="Per-shard session.sqlite paths.")
    args = parser.parse_args()

    merge(args.shards, args.out)
    merged = WorkDB(args.out, WorkDB.Mode.open)
    print(
        f"merged {len(args.shards)} shards into {args.out}: {merged.num_work_items} mutants, {merged.num_results} results",
        file=sys.stderr,
    )
    merged.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
