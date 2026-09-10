# Wizard release audit - 2026-09-10

## Scope and isolation

- Repair worktree: `wizard-audit-worktree`, detached from product commit `8e3fa92`.
- Audit commits: `1e7b7bd` (CLI/package lifecycle) and `727c746` (ingest, metadata, dependencies, release surfaces).
- No push, tag, PR, or change to the user's existing checkout/worktrees.
- External cleanroom: `/Volumes/Research_1/wizard-cleanroom-20260910`.
- Disposable runtime/config/key material lived under `/private/tmp`; the final
  `wizard delete --yes` removed `backend/.env`, config, processes, and listeners.

## Executed evidence

- Official v1.0.10 macOS arm64 archive and published SHA256 were verified before
  repair; the original archive was missing `requirements-optional.txt`.
- Rebuilt all five release targets with `scripts/build_release.sh`; native arm64
  version self-test passed and the other four targets cross-compiled.
- Final arm64 audit archive:
  `dist/Wizard-v1.0.10-audit-final-darwin-arm64.zip`
  SHA256: `3c6073d5f196ad244ce7de41871cce95e9319dc82c4b7d1f643e01e42438ec25`.
- Final archive contains the optional requirements file, currency normalizer,
  app version 1.0.10, Next.js 16.3.4, provider-utils 4.0.51, sharp 0.35.4,
  and baseline-browser-mapping 2.11.21.
- Fresh final-package install completed 110 Python packages, locked frontend
  install, and production Next build; final health returned HTTP 200 with
  API compatibility 4.0.0 and app version 1.0.10.
- Backend: `pytest -q` -> 2224 passed, 15 skipped, 2 warnings.
- CLI: `GOCACHE=/private/tmp/wizard-w2-gocache go test ./...` passed.
- Frontend production build passed; post-fix `pnpm audit --prod` reported zero
  advisories at every severity.
- Frontend `pnpm run lint` and `pnpm exec tsc --noEmit` passed; a post-version
  selected backend rerun (`test_loader.py` plus `test_api.py`) passed 98 tests.
- Live Gemini E2E on the disposable runtime uploaded `sales.csv`, generated a
  verified analysis artifact, and returned the correct total revenue `365.50`.
- The pre-fix live messy-data run changed `$125.50` to `70.0`; the deterministic
  currency normalization regression was added and the same runtime then
  preserved `125.5` after cleaning.
- Duplicate start/stop, status, port, health, API config, unsupported upload,
  malformed chat, and workspace traversal probes were executed. Traversal was
  rejected with HTTP 403; unsupported upload with HTTP 422.
- Sandbox self-test showed process restriction only on this host; filesystem,
  network, and memory restrictions are unavailable under the current macOS
  sandbox-exec/RLIMIT capabilities.

## Defects fixed

1. Released cloud/hybrid installs could not find `requirements-optional.txt`.
2. Detached daemon processes returned PID `-1`, causing false status, duplicate
   starts, and orphaned listeners on stop.
3. Interactive API-key prompts echoed credentials; terminal input is now hidden.
4. Interactive `auto` for the embedding model did not clear a saved value.
5. Model-authored cleaning could corrupt currency strings; financial-looking
   currency values are now parsed deterministically before model cleaning.
6. Product/release metadata and fallback installer surfaces reported stale
   versions; they now target v1.0.10 and expose app version 1.0.10.
7. Frontend dependency advisories were remediated and locked to patched ranges.

## Remaining release conditions

- The external exFAT SSD produced AppleDouble `._*` sidecars during dependency
  installation, and `uv` could not install the venv there. The cleanroom and
  release archives remain on the SSD, but the executable venv test used local
  APFS-backed disposable storage.
- Host execution is not kernel-isolated on this macOS version; Docker is the
  required stronger isolation path and was not exercised in this audit.
- Windows and Linux binaries were cross-compiled but not executed on this
  arm64 macOS host; PowerShell is unavailable locally.
- The separate `wizard-website` checkout still contains stale v1.0.8 and Python
  3.11 installation/documentation references and was not modified because its local
  branch is behind its remote and is outside this repair worktree.
- One repeat Gemini request from the freshly extracted final package was
  blocked by the environment's sensitive-data transmission guard; the earlier
  approved live Gemini request validated the provider flow on the same release
  code path.

## Verdict

**CONDITIONAL PASS for local macOS arm64 release readiness.** The repaired
source and final local archive pass the available executable gates, but publish
should remain blocked until the website propagation, Windows/Linux runtime
validation, and a supported isolated execution backend are addressed.
