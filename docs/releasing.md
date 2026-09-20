# Releasing Wizard

For maintainers. Everything is driven by a git tag pushed to `master`; the
[Publish Release workflow](../.github/workflows/release.yml) does the rest and
publishes nothing until every platform has installed and run the exact uploaded
files.

## Two kinds of release

| | Stable | Pre-release |
|---|---|---|
| Tag | `vX.Y.Z` | `vX.Y.Z-KIND.N`, KIND is `alpha`, `beta` or `rc`, N starts at 1 |
| GitHub | the "latest release" | flagged **pre-release**, never "latest" |
| Who gets it | everyone | only people who chose the pre-release channel |
| Homebrew, Scoop, website manifest | rendered and synced | **not** rendered, tap and website are not notified |
| Container images | `X.Y.Z` and `latest` | `X.Y.Z-KIND.N` only, `latest` does not move |
| `CHANGELOG.md` | needs a `## [vX.Y.Z]` section | not required; write the release notes on the release |

The tag's `X.Y.Z` must equal `VERSION`: a pre-release is a candidate for the
version in `VERSION`, published early. Ordering is SemVer's:
`1.0.14-alpha.1 < 1.0.14-beta.1 < 1.0.14-beta.10 < 1.0.14-rc.1 < 1.0.14`.

The grammar is enforced in four places that share one test table:
`scripts/release.py` (`tag-info`, `check`), the CLI (`cli/internal/relver`),
`scripts/install.sh` and `scripts/install.ps1`. Change it in all four.

## Cutting a stable release

1. `python scripts/release.py set-version X.Y.Z`
2. Add a `## [vX.Y.Z]` section to `CHANGELOG.md` (rename `[Unreleased]`).
3. Merge, then `git tag vX.Y.Z && git push origin vX.Y.Z` from the merged commit.
4. The workflow verifies, builds, smoke-tests five platforms, e2e-tests three,
   stages a **draft** release, renders Homebrew/Scoop/website metadata from the
   draft's `SHA256SUMS`, installs the draft on five platforms, then makes it
   public and verifies the public release. Replace the empty release notes.

## Cutting a pre-release

1. `VERSION` already says the version you are working toward (`X.Y.Z`).
2. `git tag vX.Y.Z-beta.1 && git push origin vX.Y.Z-beta.1`
3. Same pipeline, except: the release is created as a draft **pre-release**, no
   package metadata is rendered, the Homebrew and Scoop checks are skipped, and
   after publishing a step asks the live API to confirm the release is flagged
   as a pre-release and is **not** the latest release. That step fails the
   pipeline if either is false.
4. The archives carry the full version: the packaged `VERSION` file says
   `X.Y.Z-beta.1`, so `wizard --version`, the backend's `/health` and the tag all
   agree.
5. Write the release notes by hand (`gh release edit vX.Y.Z-beta.1 --notes-file ...`).

Rehearse without publishing: run the workflow by hand (`gh workflow run
release.yml --ref <branch> -f tag=vX.Y.Z-beta.1`). It builds and tests
everything for that tag and publishes nothing.

## Promoting a pre-release

There is no "promote" button, on purpose. Tag the stable release from the commit
you want (`vX.Y.Z`); it is built and tested again from that commit. The bytes are
therefore not the pre-release's bytes, which is why the pipeline runs in full
for it. Anyone on the pre-release channel is offered the stable release as soon
as it exists, because it is newer than every pre-release of the same version.

## Retracting a pre-release

`gh release delete vX.Y.Z-beta.N --cleanup-tag --yes`. Installs that already
took it keep running it; the next `wizard update` on the pre-release channel
moves them to whatever is newest.

## Why "latest" is the isolation

Every stable path (`install.sh`, `install.ps1`, `wizard update`, the Homebrew
formula, the website's `/install.sh` redirect and version display) resolves
GitHub's `releases/latest`, which excludes pre-releases and drafts. The
pre-release channel is defined as the stable release, unless a release GitHub
flags as a pre-release with a valid tag is newer. It is deliberately **not**
"the highest version in the release list": this repository still carries an
older v2.x line that outranks v1.0.x numerically and is not a pre-release.

## Known limit: the installer URLs follow the latest stable release

`https://wizardw2.vercel.app/install.sh` redirects to
`releases/latest/download/install.sh`, so it serves the installer of the latest
**stable** release. The `--pre-release` option therefore reaches new installs
once a stable release that contains it exists. Before that, install a
pre-release from its own assets:

```bash
curl -fsSL https://github.com/Wizard-AIA/Wizard-w2/releases/download/vX.Y.Z-beta.1/install.sh | sh -s -- --version X.Y.Z-beta.1
```
