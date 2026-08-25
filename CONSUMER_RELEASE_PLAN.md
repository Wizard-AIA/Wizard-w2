# Consumer Distribution for Wizard w2 — Final Plan

## Approach

**GitHub Releases from the same repo.** No separate consumer repo. The source of truth stays in `Wizard-AIA/Wizard-w2`. Consumers download a clean per-platform zip from the **GitHub Releases** page. Contributors clone the repo as they always have.

---

## Decisions

| Decision | Answer |
|---|---|
| **Frontend** | Ship source — `wizard init` runs `pnpm install && pnpm build` |
| **Workspace sessions** | Gitignore `workspace/sessions/` in the dev repo too |
| **Version scheme** | Start fresh at **`v1.0.0`** for consumer releases (independent of dev `API_VERSION`) |
| **Release format** | Per-platform zips (~15 MB each), one binary per zip |
| **Sample data** | Include sample CSVs so consumers can try immediately |
| **Release trigger** | Auto on `v*` tags via GitHub Actions |
| **Dev repo link** | Consumer README links back to `Wizard-AIA/Wizard-w2` for contributors |
| **Backend .env** | Ship `.env.example` only — `wizard init` or the user copies it |

---

## What the consumer zip contains

```
Wizard-v1.0.0/
├── backend/
│   ├── src/                  # Full API server source
│   ├── main.py               # Entry point
│   ├── prompts/              # LLM prompt templates
│   ├── skills/               # Built-in analysis skills
│   │   ├── cohort-analysis/
│   │   └── data-quality-triage/
│   ├── docker/
│   │   └── Dockerfile        # Sandbox container image
│   ├── .env.example          # Configuration reference (user copies to .env)
│   └── .gitignore
├── frontend/
│   ├── app/                  # Next.js pages
│   ├── components/           # React components
│   ├── lib/                  # Utilities & hooks
│   ├── public/               # Static assets (icons, sounds)
│   ├── package.json
│   ├── pnpm-lock.yaml
│   ├── next.config.ts
│   ├── tsconfig.json
│   ├── postcss.config.mjs
│   ├── components.json
│   ├── eslint.config.mjs     # needed by next build
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── .env.example
│   └── next-env.d.ts
├── cli/
│   └── wizard                # Pre-built binary for THIS platform
├── workspace/
│   ├── dataset.csv           # Sample dataset for trying out
│   └── housing.csv           # Sample housing data
├── Dockerfile                # Backend container image
├── docker-compose.yml        # One-command Docker launch
├── requirements.txt
├── requirements.lock.txt
├── requirements-local.txt    # Host-mode Python deps (no Docker)
├── .dockerignore
├── LICENSE
└── README.md                 # Consumer-focused
```

**Estimated size per zip: ~15 MB**

---

## What gets excluded — the `.distignore`

```gitignore
# === Tests ===
backend/tests/

# === Benchmarks & scripts ===
scripts/
workspace/sessions/

# === Documentation ===
docs/
PLAN.md
claude_plan.md
CHANGELOG.md
CONSUMER_RELEASE_PLAN.md

# === Contributor guides ===
CONTRIBUTING.md
CODE_OF_CONDUCT.md
GOVERNANCE.md
MAINTAINERS.md
SUPPORT.md
SECURITY.md

# === AI assistant configs ===
CLAUDE.md
backend/CLAUDE.md
frontend/CLAUDE.md
cli/CLAUDE.md
cli/README.md
frontend/README.md
.claude/

# === CI/CD ===
.github/

# === Linting & formatting ===
pyproject.toml
pyrightconfig.json
.editorconfig
.pre-commit-config.yaml
commitlint.config.mjs
.ruff_cache/
.lycheeignore

# === Audit & compliance ===
check-license-compliance.config.yml
.safety-project.ini
requirements-audit-tools.in
requirements-audit-tools.lock.txt
requirements-ci-tools.in
requirements-ci-tools.lock.txt

# === Mutation testing ===
cosmic-ray.toml
.cosmic-ray-scratch/

# === Dev caches ===
.pytest_cache/
frontend/.next/
frontend/node_modules/
frontend/tsconfig.tsbuildinfo

# === Dev scripts ===
backend/scripts/

# === Optional/experimental deps ===
requirements-optional.txt

# === Git internals ===
.git/
.gitattributes
.gitignore

# === Root .env (benchmark key) ===
.env

# === CLI source (binary is shipped) ===
cli/cmd/
cli/internal/
cli/go.mod
cli/go.sum
cli/.gitignore
```

---

## Release artifacts on GitHub

| Artifact | Contents |
|---|---|
| `Wizard-v1.0.0-darwin-arm64.zip` | Full zip + macOS Apple Silicon `wizard` binary |
| `Wizard-v1.0.0-darwin-amd64.zip` | Full zip + macOS Intel `wizard` binary |
| `Wizard-v1.0.0-linux-amd64.zip` | Full zip + Linux x86_64 `wizard` binary |
| `Wizard-v1.0.0-linux-arm64.zip` | Full zip + Linux ARM64 `wizard` binary |
| `Wizard-v1.0.0-windows-amd64.zip` | Full zip + Windows `wizard.exe` binary |

---

## README.md restructure

### Top half — Consumer-facing

```
# 🧙‍♂️ Wizard

> Local-first autonomous data analysis agent. Ask a question about
> your data, it investigates, and streams its reasoning as it goes.

## Download
→ Latest release: [Wizard v1.0.0](link-to-releases)
Pick the zip for your OS. Extract. Done.

## Quick start
    ./wizard init       # checks prereqs, installs deps
    ./wizard start      # launches everything, opens browser

Open http://localhost:3000

## Docker alternative
    docker compose up --build -d

## Choosing models
... (existing model section)

## Configuration
... (existing .env table)

## Troubleshooting
... (existing troubleshooting section)
```

### Bottom half — Collapsed

```markdown
<details>
<summary><h2>Contributing & Development</h2></summary>

The full developer repository with tests, benchmarks, CI pipelines,
and architecture docs is at:
**[Wizard-AIA/Wizard-w2](https://github.com/Wizard-AIA/Wizard-w2)**

See [CONTRIBUTING.md](link) for the full workflow.
</details>
```

---

## Proposed changes (files to create/modify)

### [NEW] `.distignore`
Manifest listing all dev-only paths (contents shown above).

### [NEW] `scripts/build_release.sh`
Shell script that:
1. Reads `.distignore`
2. Cross-compiles `wizard` CLI for 5 platform/arch combos via `go build`
3. For each platform: assembles a zip from repo minus ignored paths, with the correct binary at `cli/wizard`
4. Outputs to `dist/` (gitignored)

### [NEW or MODIFY] `.github/workflows/release.yml`
GitHub Actions workflow:
- **Trigger:** on push of `v*` tags
- **Steps:** checkout → run `build_release.sh` → attach 5 zips to the GitHub Release
- If a `release.yml` already exists, update it; otherwise create new

### [MODIFY] `README.md`
Restructure to consumer-first layout (download → run → use at top, dev content collapsed at bottom).

### [MODIFY] `.gitignore`
Add:
- `dist/` — local release build output
- `workspace/sessions/` — dev session data shouldn't be committed

---

## Verification plan

### Automated (part of the workflow)
- `build_release.sh` verifies the output zip contains exactly the expected files
- Each platform's CLI binary is tested with `./wizard version` after cross-compile
- Diff the zip contents against a known manifest to catch drift

### Manual
1. Extract a release zip to a clean directory
2. Run `./wizard init && ./wizard start` — confirm app launches at `localhost:3000`
3. Run `docker compose up --build -d` from the extracted zip — confirm same
4. Upload the sample `dataset.csv`, ask a question, confirm the full flow works
5. Verify the README makes sense to someone with no dev context

