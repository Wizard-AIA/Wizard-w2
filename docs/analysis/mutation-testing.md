# Mutation Testing

`cosmic-ray` (pinned in `requirements-ci-tools.in`) measures whether the test suite actually
kills faulty variants of the analytical control plane's deterministic modules, rather than just
achieving line coverage.

## Why cosmic-ray, not mutmut

`mutmut` was tried first and rejected after verification, not preference: it hard-asserts a
mutated module's dotted import name never starts with `src.` (`record_trampoline_hit` in
`mutmut/__main__.py`, confirmed unchanged from the pinned 3.3.1 through the latest 3.7.0 release
on PyPI). This backend's own convention — `pythonpath = ["backend"]` in `pyproject.toml`, every
module imported as `src.*` — trips that assertion on every single mutant, in both the pinned and
the newest mutmut release. `cosmic-ray` has no equivalent restriction: it mutates the real file on
disk in place for the duration of one test run (via `ast`, then reverts), rather than generating a
parallel `mutants/` sandbox tree with statically-injected trampoline functions keyed by dotted
module name, so the collision never arises. Verified end-to-end against this repository: a scoped
session against `grounding.py` + `actions.py` (514 discovered mutants) produced real `KILLED`/
`SURVIVED` outcomes with no tooling errors.

## Scope

`cosmic-ray.toml` at the repo root sets `module-path` to:

- `backend/src/core/agent/grounding.py`
- `backend/src/core/agent/actions.py`

(growing as each phase adds a module to `backend/src/core/analysis/`). `test-command` runs the
full `backend/tests` suite from the repo root — cosmic-ray mutates the file in place, so no
`also_copy`/sandbox-completeness bookkeeping is needed the way a mutmut config would require.

## Not a CI gate (this PR)

Run manually/locally, not wired into `.github/workflows/ci.yml`. A CI gate needs a stable baseline
survival rate and a triage process for genuinely-equivalent mutants — premature before the module
set stabilises across all fifteen phases. Revisit once Phase 14 lands.

## Running it

```bash
# from the repo root
cosmic-ray init cosmic-ray.toml session.sqlite    # discovers mutants, one row per mutant
cosmic-ray baseline cosmic-ray.toml                # confirms the unmutated suite passes first
cosmic-ray exec cosmic-ray.toml session.sqlite     # runs the suite once per mutant
cosmic-ray dump session.sqlite | cr-report          # or read directly:
python -c "
import sqlite3
conn = sqlite3.connect('session.sqlite')
print(conn.execute('select test_outcome, count(*) from work_results group by test_outcome').fetchall())
"
```

`session.sqlite` (or whatever name is chosen) is a local scratch file — **never commit it**; it
and any `*.sqlite` session database from a mutation run are gitignored at the repo root.

Full-suite `test-command` makes each mutant run cost roughly one full `pytest backend/tests`
invocation; for a fast interactive check against a narrower slice while iterating, point
`test-command` at the specific unit test file(s) covering the module under change (e.g.
`python -m pytest -x -q backend/tests/unit/test_grounding.py`) — this trades completeness for
speed and is not the config committed here, which stays scoped to the full suite for a trustworthy
survival rate.

Each phase that adds a module to `module-path` is expected to also get its survival rate checked
and gaps closed with new test cases before that phase's commit, not deferred.
