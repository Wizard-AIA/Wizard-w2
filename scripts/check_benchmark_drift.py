"""Verify the versioned, offline analytical-quality benchmark baseline.

This deliberately does not run a live model or manufacture a pass rate. The
standard pytest job executes the adversarial scenarios; this companion check
then verifies that their fixture identities and required test coverage still
match the reviewed baseline. Changing a scenario is allowed, but requires an
explicit baseline update in the same review.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from benchmark_harness.baseline import compare, fingerprint_manifest, load_baseline


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "benchmarks" / "baselines" / "offline_adversarial_v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--print-current",
        action="store_true",
        help="Print the reviewed fixture/test manifest; useful when intentionally updating a baseline.",
    )
    args = parser.parse_args()

    current = fingerprint_manifest(ROOT)
    if args.print_current:
        print(json.dumps(current, indent=2, sort_keys=True))
        return 0

    try:
        expected = load_baseline(args.baseline)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BENCHMARK BASELINE INVALID: {exc}", file=sys.stderr)
        return 2

    failures = compare(expected, current)
    if failures:
        print("BENCHMARK REGRESSION DETECTED:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(
        "Offline analytical benchmark baseline matches "
        f"{len(current['scenarios'])} reviewed scenario fixtures and required tests."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
