"""Partitions backend/src's Python files into N cosmic-ray shards for CI.

One cosmic-ray session over the whole backend is not workable as a single CI
job: a scoped run against two files (grounding.py + actions.py, 587 lines)
already produced 514 mutants, and the full pytest suite -- which is what
cosmic-ray re-runs once per mutant -- costs several seconds per run even
in-process. Extrapolated (and confirmed against the full tree: ~23k mutants),
a single sequential run is on the order of a day. Splitting the file list
into N independent cosmic-ray sessions, run as parallel matrix jobs and
merged afterward (see merge_sessions.py), is what makes "the whole backend"
actually finish in scheduled-CI time.

Output is a JSON array of shards, each a list of file paths, sized by line
count (a cheap proxy for mutant count -- computing the real count means
running `cosmic-ray init` per file, which is not worth the cost of a plan
step meant to run in seconds) via longest-processing-time-first bin packing,
so shards finish in roughly the same wall-clock time rather than one shard
drawing every large file.
"""

from __future__ import annotations

import argparse
import json
import sys
from fnmatch import fnmatch
from pathlib import Path


def collect_files(roots: list[str], excludes: list[str]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        root_path = Path(root)
        if root_path.is_file():
            candidates = [root_path]
        else:
            candidates = sorted(root_path.rglob("*.py"))
        for path in candidates:
            posix = path.as_posix()
            if any(fnmatch(posix, pattern) for pattern in excludes):
                continue
            files.append(path)
    return files


def plan_shards(files: list[Path], shard_count: int) -> list[list[str]]:
    shard_count = max(1, min(shard_count, len(files) or 1))
    weighted = sorted(((f.stat().st_size, f) for f in files), reverse=True)
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0] * shard_count
    for size, path in weighted:
        lightest = totals.index(min(totals))
        shards[lightest].append(path.as_posix())
        totals[lightest] += size
    # Empty shards (more shards requested than makes sense for the weight
    # distribution) are dropped rather than sent to a matrix job with nothing
    # to mutate, which cosmic-ray's `init` would just fail on.
    return [shard for shard in shards if shard]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", nargs="+", default=["backend/src"])
    parser.add_argument("--exclude", nargs="*", default=["*/__init__.py"])
    parser.add_argument("--shards", type=int, default=20)
    args = parser.parse_args()

    files = collect_files(args.roots, args.exclude)
    if not files:
        print("no files matched --roots/--exclude", file=sys.stderr)
        return 1

    shards = plan_shards(files, args.shards)
    # {index, files} objects, not a bare list of file-lists, so a GitHub
    # Actions matrix job can address `matrix.shard.index` (for a readable job
    # name/artifact name) alongside `matrix.shard.files`.
    matrix = [{"index": i, "files": shard} for i, shard in enumerate(shards)]
    print(json.dumps(matrix, separators=(",", ":")))
    print(f"{len(files)} files across {len(shards)} shards", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
