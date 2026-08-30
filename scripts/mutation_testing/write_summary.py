"""Writes a short Markdown summary of a merged cosmic-ray session.

cosmic-ray ships cr-html/cr-report/cr-rate for a full report, but none of
them produce the "which files need more tests" table this CI job actually
wants in `$GITHUB_STEP_SUMMARY` -- this reads the same WorkDB they do and
groups survived mutants by file instead.

A mutant with no test_outcome (the worker crashed, timed out, or found no
test to run at all) is not "survived" -- it is incomplete, and reported
separately so it does not silently inflate the killed count the way treating
"not survived" as "killed" would.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from cosmic_ray.work_db import WorkDB
from cosmic_ray.work_item import TestOutcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session")
    parser.add_argument("--out", required=True)
    parser.add_argument("--worst-files", type=int, default=20)
    args = parser.parse_args()

    db = WorkDB(args.session, WorkDB.Mode.open)
    results = dict(db.results)

    killed = survived = incomplete = 0
    per_file_total: Counter[str] = Counter()
    per_file_survived: Counter[str] = Counter()

    for item in db.work_items:
        module_path = str(item.mutations[0].module_path) if item.mutations else "?"
        per_file_total[module_path] += 1
        result = results.get(item.job_id)
        if result is None or result.test_outcome is None:
            incomplete += 1
            continue
        if result.test_outcome == TestOutcome.SURVIVED:
            survived += 1
            per_file_survived[module_path] += 1
        else:
            killed += 1

    scored = killed + survived
    rate = (survived / scored * 100) if scored else 0.0

    lines = [
        "## Mutation testing report (backend)",
        "",
        f"**{db.num_work_items}** mutants total across the scanned files.",
        f"- Killed: **{killed}**",
        f"- Survived: **{survived}** ({rate:.1f}% survival rate of the {scored} scored mutants)",
        f"- Incomplete (worker crash/timeout/no matching test): **{incomplete}**",
        "",
    ]

    worst = per_file_survived.most_common(args.worst_files)
    if worst:
        lines += [
            "### Files with the most surviving mutants",
            "",
            "A surviving mutant means the test suite did not notice that line's behavior change -- the most direct signal of an undertested branch.",
            "",
            "| File | Survived | Total mutants |",
            "|---|---:|---:|",
        ]
        for module_path, count in worst:
            lines.append(f"| `{module_path}` | {count} | {per_file_total[module_path]} |")
    else:
        lines.append("No surviving mutants.")

    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"killed={killed} survived={survived} incomplete={incomplete} rate={rate:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
