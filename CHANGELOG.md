# Changelog

All notable changes to Wizard are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions before this
file existed are reconstructed from tags, release notes, and milestone commits.

## [Unreleased]

### Changed
- Backend dependency installs (CI, Dockerfiles, `wizard init`/`update`) switched
  from `pip` to `uv`; frontend switched from `npm` to `pnpm`, with
  `frontend/pnpm-lock.yaml` replacing `frontend/package-lock.json`. Hash-pinned
  lock files are unchanged -- they were already `uv pip compile` output.
- Migrated the project from a personal account to the `Wizard-AIA` GitHub
  organization; core repo renamed `Wizard-w1` → `Wizard-w2`.
- Hardened repo governance and CI/CD: Go CI for the `cli/` daemon, CodeQL
  coverage for Go, Dependabot version-bump PRs, SBOM generation on release,
  OSSF Scorecard, `GOVERNANCE.md`/`SUPPORT.md`, and an org-wide docs site.
- Replaced `gitleaks-action` (requires a paid license under an org) with the
  free `gitleaks` CLI run directly in CI.

## [v2.0.0-w2-planning] - 2026-08-07

Wizard w2 — a from-the-ground-up evolution of the w1 codebase, delivered as
ten milestones (see `docs/wizard-evolution-spec.md` for the full spec):

1. Provider-agnostic model layer with an explicit local/cloud/hybrid data mode
2. Two independent dials — agent depth and permission profile
3. Host-primary execution with Docker made optional, OS-native sandboxing
   (Landlock/seccomp on Linux, `sandbox-exec` on macOS, a job object on Windows)
4. Expanded data connectivity — relational databases and object storage
5. A `SKILL.md`-based skills system the agent can cite and promote to
6. A GitHub-based public skill registry, with install/update/diff review
7. Subagents for parallel, isolated sub-investigations
8. `wizard`, a single static Go binary managing the stack as a background
   service, replacing the manual `uvicorn`/`npm run dev`/`docker compose` dance
9. Re-runnable export (script/notebook) and unified results actions
10. Versioning, docs, and release polish for the w2 line

### Added
- `MAINTAINERS.md`, upstream URL fixes for the org migration.

## [v2.2.1] - 2026-01-30
Dependency bumps and CI housekeeping.

## [v2.2.0] - 2026-01-29
### Added
- Documentation link-checking in CI (Lychee).
### Changed
- CI Action version bumps.

## [v2.1.1] - 2026-01-29
### Added
- Repository CI/CD, code-quality, and automation workflows; README badges.
### Changed
- README clarity pass; model-availability and Docker-prerequisite disclaimers.

## [v2.1.0] - 2026-01-29
### Added
- `MODEL_PATH` environment variable and a read-only backend volume mount.

## [v2.0.0] - 2026-01-29
Initial public foundation: FastAPI backend (CSV upload, chat, validation),
the first agent framework and skills, and the CI/CD bootstrap (linting,
dependency auditing, API contract tests).

[Unreleased]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.0.0-w2-planning...HEAD
[v2.0.0-w2-planning]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.2.1...v2.0.0-w2-planning
[v2.2.1]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.2.0...v2.2.1
[v2.2.0]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.1.1...v2.2.0
[v2.1.1]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.1.0...v2.1.1
[v2.1.0]: https://github.com/Wizard-AIA/Wizard-w2/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/Wizard-AIA/Wizard-w2/releases/tag/v2.0.0
