#!/usr/bin/env bash
# Builds the consumer release zips: cross-compiles the wizard CLI for five
# platform/arch targets, then assembles a zip per target from a clean git
# checkout (not the working tree -- a maintainer's local .venv/node_modules/
# build caches must never leak into a shipped zip) minus the paths listed in
# .distignore, with that platform's binary at cli/wizard (cli/wizard.exe on
# Windows).
#
# Usage: scripts/build_release.sh [version]
#   version defaults to the current commit's exact tag, or "dev" otherwise.
#
# Output: dist/Wizard-<version>-<goos>-<goarch>.zip, one per target.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-$(git describe --tags --exact-match 2>/dev/null || echo dev)}"
DIST_DIR="$REPO_ROOT/dist"
BASE_STAGE="$DIST_DIR/_base"

API_VERSION="$(sed -nE 's/^API_VERSION = "([^"]+)"/\1/p' backend/src/api/routes/meta.py | head -1)"
if [ -z "$API_VERSION" ]; then
  echo "error: could not read API_VERSION from backend/src/api/routes/meta.py" >&2
  exit 1
fi

TARGETS=(
  "darwin arm64"
  "darwin amd64"
  "linux amd64"
  "linux arm64"
  "windows amd64"
)

HOST_GOOS="$(go env GOOS)"
HOST_GOARCH="$(go env GOARCH)"

rm -rf "$DIST_DIR"
mkdir -p "$BASE_STAGE"

echo "Archiving HEAD (tracked files only) into $BASE_STAGE..."
git archive HEAD | tar -x -C "$BASE_STAGE"

echo "Removing .distignore paths from the staged tree..."
while IFS= read -r entry; do
  case "$entry" in
    ''|'#'*) continue ;;
  esac
  target="$BASE_STAGE/${entry%/}"
  if [ -e "$target" ]; then
    rm -rf "$target"
  fi
done < "$REPO_ROOT/.distignore"

# .distignore is a plain list of literal paths (no globs), so the loop above
# is an exact match per line -- verify nothing in it silently missed its
# target, which would mean the manifest and .distignore have drifted apart.
while IFS= read -r entry; do
  case "$entry" in
    ''|'#'*) continue ;;
  esac
  if [ -e "$BASE_STAGE/${entry%/}" ]; then
    echo "error: .distignore entry '$entry' still present after removal" >&2
    exit 1
  fi
done < "$REPO_ROOT/.distignore"

# requirements.txt/.lock.txt/-local.txt pin this org's own Safety.dev
# vulnerability-scanning proxy as their package index (see
# .safety-project.ini) -- fine for this team's own CI, but a consumer's
# `wizard init`/`docker compose up` must not depend on that account-scoped
# mirror staying reachable to the public forever. Strip the index-url
# declarations so installs fall back to public PyPI; every `--hash=...` line
# is left untouched, since those pin the wheel's actual bytes, not where it
# came from, and Safety's proxy is a passthrough of the same public wheels.
echo "Rewriting requirements files to drop the internal package index..."
for req_file in requirements.txt requirements.lock.txt requirements-local.txt; do
  path="$BASE_STAGE/$req_file"
  [ -f "$path" ] || continue
  sed -i.bak -E '/^(-i |--index-url|--extra-index-url)/d' "$path"
  rm -f "$path.bak"
done
if grep -rl "pkgs.safetycli.com" "$BASE_STAGE" >/dev/null 2>&1; then
  echo "error: pkgs.safetycli.com still referenced in the staged tree after rewriting -- update the loop above" >&2
  grep -rl "pkgs.safetycli.com" "$BASE_STAGE" >&2
  exit 1
fi

MANIFEST_FILE="$DIST_DIR/manifest-base.txt"
(cd "$BASE_STAGE" && find . -type f | sed 's|^\./||' | sort) > "$MANIFEST_FILE"
base_file_count="$(wc -l < "$MANIFEST_FILE" | tr -d ' ')"
echo "Staged base tree: $base_file_count files (see $MANIFEST_FILE)."

for target in "${TARGETS[@]}"; do
  read -r goos goarch <<< "$target"
  binname="wizard"
  [ "$goos" = "windows" ] && binname="wizard.exe"

  build_dir="$DIST_DIR/_build-$goos-$goarch"
  mkdir -p "$build_dir"

  echo
  echo "Building cli/$binname for $goos/$goarch..."
  (cd "$REPO_ROOT/cli" && CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" go build \
    -ldflags "-X wizard/internal/compat.CompatAPIVersion=$API_VERSION -X wizard/internal/compat.BuildVersion=$VERSION" \
    -o "$build_dir/$binname" ./cmd/wizard)

  if [ "$goos" = "$HOST_GOOS" ] && [ "$goarch" = "$HOST_GOARCH" ]; then
    echo "Native target -- self-testing: $binname version"
    "$build_dir/$binname" version
  else
    echo "Cross-compiled for $goos/$goarch; not runnable on this $HOST_GOOS/$HOST_GOARCH host, so skipping execution (build success is the only signal available)."
  fi

  archive_name="Wizard-$VERSION-$goos-$goarch"
  archive_root="$build_dir/$archive_name"
  mkdir -p "$archive_root/cli"
  cp -R "$BASE_STAGE"/. "$archive_root"/
  cp "$build_dir/$binname" "$archive_root/cli/$binname"
  chmod +x "$archive_root/cli/$binname"

  zip_path="$DIST_DIR/$archive_name.zip"
  (cd "$build_dir" && zip -r -q "$zip_path" "$archive_name")

  actual_file_count="$(unzip -Z1 "$zip_path" | grep -vc '/$')"
  expected_file_count=$((base_file_count + 1))
  if [ "$actual_file_count" -ne "$expected_file_count" ]; then
    echo "error: $zip_path has $actual_file_count files, expected $expected_file_count (staged base + 1 binary) -- .distignore and the zip contents have drifted apart" >&2
    exit 1
  fi

  size="$(du -h "$zip_path" | cut -f1)"
  echo "Wrote $zip_path ($size, $actual_file_count files)"

  rm -rf "$build_dir"
done

# The updater verifies this file before unpacking an archive. Keep it a
# release asset, not merely CI output, so every client receives the same
# digest list as the publisher.
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$DIST_DIR" && sha256sum ./*.zip > SHA256SUMS)
else
  (cd "$DIST_DIR" && shasum -a 256 ./*.zip > SHA256SUMS)
fi

echo
echo "All targets built. Zips in $DIST_DIR:"
ls -lh "$DIST_DIR"/*.zip
