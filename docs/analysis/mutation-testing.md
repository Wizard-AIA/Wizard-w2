# Mutation Testing

Two tools, one per language, both wired into CI as reports rather than merge gates:

| | Backend (Python) | CLI (Go) |
| :-- | :-- | :-- |
| Tool | `cosmic-ray` (pinned in `requirements-ci-tools.in`) | `go-gremlins/gremlins`, pinned by tag in the workflow |
| Scope | all of `backend/src` | the whole `cli` module |
| Cadence | weekly (`schedule`) + `workflow_dispatch` | every push/PR touching `cli/` (`cli-ci.yml`) |
| Workflow | `.github/workflows/mutation-testing.yml` | `.github/workflows/cli-ci.yml`'s `mutation-testing` job |
| Report | step summary + `mutation-report` artifact (sqlite/HTML/text/markdown) | step summary + `gremlins-report` artifact (JSON/text) |

Neither gates a merge yet. See [Not a CI gate](#not-a-ci-gate-yet) for why.

## Backend: cosmic-ray

### Why cosmic-ray, not mutmut

`mutmut` was tried first and rejected after verification, not preference: it hard-asserts a
mutated module's dotted import name never starts with `src.` (`record_trampoline_hit` in
`mutmut/__main__.py`, confirmed unchanged from the pinned 3.3.1 through the latest 3.7.0 release
on PyPI). This backend's own convention — `pythonpath = ["backend"]` in `pyproject.toml`, every
module imported as `src.*` — trips that assertion on every single mutant, in both the pinned and
the newest mutmut release. `cosmic-ray` has no equivalent restriction: it mutates the real file on
disk in place for the duration of one test run (via `ast`, then reverts), rather than generating a
parallel `mutants/` sandbox tree with statically-injected trampoline functions keyed by dotted
module name, so the collision never arises.

### Scope

`cosmic-ray.toml` at the repo root sets `module-path = ["backend/src"]` — the whole backend, not
just the analytical control plane's modules as in earlier phases. `test-command` runs the full
`backend/tests` suite from the repo root; cosmic-ray mutates the file in place, so no
`also_copy`/sandbox-completeness bookkeeping is needed the way a mutmut config would require.

### Why this can't run as one job

Measured directly against this repository: a scoped session against two files (`grounding.py` +
`actions.py`, 587 lines combined) produced 514 mutants; `cosmic-ray init` against the whole of
`backend/src` (31k lines) produced **23,334**. Each mutant costs roughly one full
`pytest backend/tests` invocation — measured at ~5–6 seconds per mutant on a GitHub-hosted
runner — so a single sequential pass is on the order of **a day**, not something any per-PR or
even nightly single job can absorb. cosmic-ray's own `local` distributor runs strictly
sequentially (verified by reading `cosmic_ray.distribution.local`); the only way to parallelize
without switching distributor architecture entirely is to run *multiple independent sessions*
concurrently.

### How the CI workflow makes it work

`.github/workflows/mutation-testing.yml`:

1. **`plan`** — `scripts/mutation_testing/plan_shards.py` walks `backend/src`, excludes
   `__init__.py`, and bin-packs the remaining files into N shards (default 20, overridable via
   `workflow_dispatch`) balanced by file size (a cheap proxy for mutant count — computing the real
   count means running `cosmic-ray init` per file, not worth it for a planning step meant to take
   seconds). Output feeds a job matrix as `{index, files}` objects.
2. **`mutate`** (matrix, `fail-fast: false`) — each shard renders its own `cosmic-ray.toml`
   (`scripts/mutation_testing/render_shard_config.py`, scoped to just its file list; `test-command`
   is unchanged — still the full suite, since a mutation to file X can be caught by a test living
   anywhere in the tree, not only in an obviously-related test file), runs `baseline` → `init` →
   `exec`, and uploads its `session.sqlite` as an artifact.
3. **`report`** — downloads every shard's session, merges them into one
   (`scripts/mutation_testing/merge_sessions.py`, via cosmic-ray's own `WorkDB` API rather than raw
   SQL, so it survives schema changes across cosmic-ray versions — safe because each shard's job
   IDs are independently-minted UUIDs with nothing to collide on), then:
   - `scripts/mutation_testing/write_summary.py` writes a killed/survived/incomplete breakdown plus
     a "files with the most surviving mutants" table to `$GITHUB_STEP_SUMMARY`.
   - `cr-html`/`cr-report --surviving-only` render the full interactive/text reports.
   - Everything (merged `.sqlite`, HTML, text, markdown summary) is uploaded as one `mutation-report`
     artifact, retained 90 days.

With 20 shards, ~23k mutants at ~5.5s each is roughly **1,150 mutants × 5.5s ≈ 1.75 hours per
shard** — well inside a single GitHub-hosted job's default 6-hour timeout (`timeout-minutes: 300`
is set explicitly as a backstop). Matrix concurrency is capped by the plan/org (20 on GitHub Free);
raise or lower `shards` via `workflow_dispatch` to match what's actually available, trading wall
time against concurrent-job budget.

### Running it locally

```bash
# The whole backend, sequentially -- slow (see above), useful for one file/directory at a time:
cosmic-ray init cosmic-ray.toml session.sqlite     # or point --module-path at one file first
cosmic-ray baseline cosmic-ray.toml                # confirms the unmutated suite passes
cosmic-ray exec cosmic-ray.toml session.sqlite
cr-report session.sqlite                            # or: cr-html session.sqlite > report.html

# The same shard/merge pipeline CI runs, for a faster local slice:
python scripts/mutation_testing/plan_shards.py --roots backend/src/core/analysis --shards 4
python scripts/mutation_testing/render_shard_config.py --files-json '[...]' --out shard.toml
cosmic-ray baseline shard.toml && cosmic-ray init shard.toml s.sqlite && cosmic-ray exec shard.toml s.sqlite
python scripts/mutation_testing/write_summary.py s.sqlite --out summary.md
```

`session.sqlite` (or any name chosen locally) is scratch — **never commit it**; `*.sqlite` is
gitignored at the repo root, along with the other generated files this tooling produces
(`cosmic-ray.shard.toml`, `/shards.json`, `/summary.md`, `mutation-report.html`,
`survived-mutants.txt`).

## CLI: gremlins

`go-gremlins/gremlins` runs against the whole `cli` Go module. Unlike cosmic-ray, it's cheap enough
to run on every push/PR (measured: a few seconds to run, since it only mutates lines the test suite
actually *covers* and runs each package's own tests rather than a project-wide suite) — no
sharding, no scheduling needed. It's wired into `cli-ci.yml` as its own `mutation-testing` job,
Ubuntu-only (mutant behavior doesn't vary by OS the way `cli`'s process-supervision code itself
does, so the existing 3-OS `build-test` matrix would triple this for no extra signal).

The job writes a killed/lived/not-covered/timed-out breakdown plus a surviving-mutants table to the
step summary, and uploads the raw JSON/text report as a `gremlins-report` artifact. It recomputes
those counts from each mutation's own `status` field rather than trusting gremlins' top-level
`mutants_total`/`mutants_killed`/etc fields — verified against a real run that those fields silently
exclude `TIMED OUT` mutants entirely (118 timed out, present per-mutation, absent from every
top-level count — which would otherwise report a misleading 100% test efficacy on a run that
mostly timed out).

Whole-module coverage is low (single digits, percent) because process-supervision code
(`start`/`stop`/`supervise`, real subprocess spawning) is intentionally not unit-tested — see
`cli/CLAUDE.md`. gremlins only mutates covered lines, so this doesn't slow the run down; it does
mean most of the module reports `NOT COVERED` rather than `KILLED`/`LIVED`, which is expected, not
a regression.

Run it locally the same way CI does:

```bash
cd cli
go run github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0 unleash . -o gremlins-report.json
```

## Not a CI gate (yet)

Neither workflow fails a run on survived mutants or a coverage/efficacy threshold. A gate needs a
stable baseline survival rate and a triage process for genuinely-equivalent mutants first —
premature before either language's report has been read even once. Revisit once a baseline
survival rate has actually been observed and reviewed; `cr-rate --fail-over` (backend) and
`gremlins unleash --threshold-efficacy`/`--threshold-mcover` (CLI) are exactly the flags that turn
this into a gate when that day comes.

Each phase/change that adds meaningful new deterministic logic is expected to get its own survival
rate checked and gaps closed with new test cases, not deferred indefinitely — the CI report exists
so that happens from real numbers instead of guesswork.
