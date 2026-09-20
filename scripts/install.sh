#!/bin/sh
# Wizard installer for macOS and Linux.
#
#   curl -fsSL https://wizardw2.vercel.app/install.sh | sh
#   curl -fsSL https://wizardw2.vercel.app/install.sh | sh -s -- --version 1.0.13 --no-modify-path
#   curl -fsSL https://wizardw2.vercel.app/install.sh | sh -s -- --pre-release
#
# Installs a published release into a per-user directory (default ~/.wizard):
# no root, no package manager. It verifies the archive against the release's
# SHA256SUMS before unpacking anything, and is safe to run again.
#
# wizard-targets: darwin-arm64 darwin-amd64 linux-amd64 linux-arm64
#
# Options (each also has an environment variable):
#   --version X.Y.Z     install this release instead of the latest   WIZARD_VERSION
#                       (also X.Y.Z-beta.N for a pre-release)
#   --pre-release       install the newest pre-release if one is newer than the
#                       latest stable, and follow pre-releases from now on
#                                                                    WIZARD_CHANNEL=pre-release
#   --channel NAME      stable (the default) or pre-release
#   --install-dir DIR   install into DIR (default ~/.wizard)         WIZARD_INSTALL_DIR
#   --no-modify-path    do not edit shell startup files              WIZARD_NO_MODIFY_PATH=1
#   --force             install even if another Wizard is on PATH
#   --verbose           show what the installer is doing             WIZARD_VERBOSE=1
#   --yes               accepted for scripts; the installer never prompts
#   -h, --help
# Advanced:
#   WIZARD_RELEASE_BASE_URL   fetch <base>/<tag>/<files> instead of GitHub Releases
#                             (an internal mirror, an air-gapped copy, or tests)
#
# Exit codes: 0 ok, 1 failure, 2 bad usage, 3 unsupported platform or missing
# tool, 4 network error.

set -eu

REPO="Wizard-AIA/Wizard-w2"
SUPPORTED="darwin-arm64 darwin-amd64 linux-amd64 linux-arm64"
RC_START="# >>> wizard (managed by the Wizard installer) >>>"
RC_END="# <<< wizard <<<"

# ---- output ------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
  BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"; RED="$(printf '\033[31m')"
  GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"; RESET="$(printf '\033[0m')"
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

say()   { printf '%s\n' "$*"; }
step()  { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()    { printf '%s ok%s  %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '%swarn%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
debug() { [ "$VERBOSE" = 1 ] && printf '%s  . %s%s\n' "$DIM" "$*" "$RESET" >&2 || true; }
die()   { code=$1; shift; printf '%serror%s %s\n' "$RED" "$RESET" "$*" >&2; exit "$code"; }

# ---- arguments ---------------------------------------------------------------

VERSION_ARG="${WIZARD_VERSION:-}"
CHANNEL="${WIZARD_CHANNEL:-}"   # "" (not chosen), stable or pre-release
INSTALL_DIR="${WIZARD_INSTALL_DIR:-${WIZARD_HOME:-}}"
NO_MODIFY_PATH="${WIZARD_NO_MODIFY_PATH:-0}"
VERBOSE="${WIZARD_VERBOSE:-0}"
FORCE=0

# Printed from here, not read back from $0: under `curl | sh` there is no file.
usage() {
  cat <<'EOF'
Wizard installer for macOS and Linux

  curl -fsSL https://wizardw2.vercel.app/install.sh | sh
  curl -fsSL https://wizardw2.vercel.app/install.sh | sh -s -- --version 1.0.13
  curl -fsSL https://wizardw2.vercel.app/install.sh | sh -s -- --pre-release

Options (each also has an environment variable):
  --version X.Y.Z     install this release instead of the latest   WIZARD_VERSION
                      (also X.Y.Z-beta.N for a pre-release)
  --pre-release       install the newest pre-release if it is newer than the
                      latest stable, and follow pre-releases from now on
                                                                   WIZARD_CHANNEL=pre-release
  --channel NAME      stable (the default) or pre-release
  --install-dir DIR   install into DIR (default ~/.wizard)         WIZARD_INSTALL_DIR
  --no-modify-path    do not edit shell startup files              WIZARD_NO_MODIFY_PATH=1
  --force             install even if another Wizard is on PATH
  --verbose           show what the installer is doing             WIZARD_VERBOSE=1
  --yes               accepted for scripts; the installer never prompts
  -h, --help          show this help

Advanced:
  WIZARD_RELEASE_BASE_URL   fetch <base>/<tag>/<files> from a mirror instead of GitHub

Exit codes: 0 ok, 1 failure, 2 bad usage, 3 unsupported platform or missing tool, 4 network error.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version)         [ $# -ge 2 ] || die 2 "--version needs a value (for example 1.0.13)"; VERSION_ARG=$2; shift 2 ;;
    --version=*)       VERSION_ARG=${1#--version=}; shift ;;
    --pre-release)     CHANNEL=pre-release; shift ;;
    --channel)         [ $# -ge 2 ] || die 2 "--channel needs a value (stable or pre-release)"; CHANNEL=$2; shift 2 ;;
    --channel=*)       CHANNEL=${1#--channel=}; shift ;;
    --install-dir)     [ $# -ge 2 ] || die 2 "--install-dir needs a directory"; INSTALL_DIR=$2; shift 2 ;;
    --install-dir=*)   INSTALL_DIR=${1#--install-dir=}; shift ;;
    --no-modify-path)  NO_MODIFY_PATH=1; shift ;;
    --force)           FORCE=1; shift ;;
    --verbose)         VERBOSE=1; shift ;;
    --yes|-y)          shift ;;
    -h|--help)         usage; exit 0 ;;
    *)                 die 2 "unknown option: $1 (see --help)" ;;
  esac
done

case "$CHANNEL" in
  ""|stable|pre-release) ;;
  prerelease) CHANNEL=pre-release ;;
  *) die 2 "unknown channel '$CHANNEL' (use stable or pre-release)" ;;
esac

[ -n "${HOME:-}" ] || die 3 "\$HOME is not set; cannot choose an install location"
[ -n "$INSTALL_DIR" ] || INSTALL_DIR="$HOME/.wizard"
case "$INSTALL_DIR" in /*) ;; *) INSTALL_DIR="$(pwd)/$INSTALL_DIR" ;; esac
case "$INSTALL_DIR" in
  /|"$HOME") die 2 "refusing to install into $INSTALL_DIR; choose a dedicated directory (default: $HOME/.wizard)" ;;
  *[\"\$\`\\]*) die 2 "the install directory may not contain a double quote, dollar sign, backtick or backslash: $INSTALL_DIR" ;;
esac

# ---- platform ----------------------------------------------------------------

detect_os() {
  case "$(uname -s)" in
    Linux)  echo linux ;;
    Darwin) echo darwin ;;
    MINGW*|MSYS*|CYGWIN*) die 3 "this is a Windows shell. Use PowerShell instead:  irm https://wizardw2.vercel.app/install.ps1 | iex" ;;
    *)      die 3 "unsupported operating system: $(uname -s). Supported: $SUPPORTED" ;;
  esac
}

detect_arch() {
  machine="$(uname -m)"
  case "$machine" in
    x86_64|amd64)  arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) die 3 "unsupported CPU architecture: $machine. Supported: $SUPPORTED" ;;
  esac
  # A shell running under Rosetta reports x86_64 on an Apple Silicon Mac; the
  # native build is the right one.
  if [ "$1" = darwin ] && [ "$arch" = amd64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = 1 ]; then
    debug "running under Rosetta; selecting the native arm64 build"
    arch=arm64
  fi
  echo "$arch"
}

OS="$(detect_os)"
ARCH="$(detect_arch "$OS")"
TARGET="$OS-$ARCH"
case " $SUPPORTED " in
  *" $TARGET "*) ;;
  *) die 3 "no Wizard release is published for $TARGET. Supported: $SUPPORTED" ;;
esac

# ---- tools -------------------------------------------------------------------

if command -v curl >/dev/null 2>&1; then DL=curl
elif command -v wget >/dev/null 2>&1; then DL=wget
else die 3 "neither curl nor wget was found. Install one (for example: apt install curl, dnf install curl, apk add curl) and re-run."
fi

BASE_OVERRIDE="${WIZARD_RELEASE_BASE_URL:-}"
BASE_OVERRIDE="${BASE_OVERRIDE%/}"
case "$BASE_OVERRIDE" in
  http://*) warn "WIZARD_RELEASE_BASE_URL is plain http: downloads are protected only by the checksum file served from the same place" ;;
esac

# fetch URL DEST: download to a file.
fetch() {
  debug "GET $1"
  if [ "$DL" = curl ]; then
    if [ -n "$BASE_OVERRIDE" ]; then
      curl -fSL --retry 3 --retry-delay 2 -o "$2" "$1"
    else
      curl -fSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 -o "$2" "$1"
    fi
  else
    wget -q --tries=3 -O "$2" "$1"
  fi
}

fetch_or_die() {
  fetch "$1" "$2" 2>"$TMP/fetch.err" || {
    [ "$VERBOSE" = 1 ] && cat "$TMP/fetch.err" >&2
    die 4 "download failed: $1
       Check your network or proxy settings (HTTPS_PROXY / NO_PROXY are honoured), then re-run."
  }
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$1" | awk '{print $NF}'
  else die 3 "cannot verify the download: none of sha256sum, shasum or openssl was found. Install one and re-run (the installer will not install unverified code)."
  fi
}

# ---- resolve the release -----------------------------------------------------

TMP="$(mktemp -d "${TMPDIR:-/tmp}/wizard-install.XXXXXX")" || die 1 "could not create a temporary directory"
cleanup() { rm -rf "$TMP"; [ -z "${STAGE:-}" ] || rm -rf "$STAGE"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

# The release grammar, shared with scripts/release.py, the CLI and install.ps1:
#   X.Y.Z                     a stable release
#   X.Y.Z-KIND.N              a pre-release (KIND is alpha, beta or rc; N starts at 1)
# No leading zeros, no build metadata, nothing else.
normalize_tag() {
  raw=${1#v}
  if ! printf '%s\n' "$raw" | awk '/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(alpha|beta|rc)\.[1-9][0-9]*)?$/ { ok=1 } END { exit(ok ? 0 : 1) }'; then
    die 2 "not a release version: '$1' (expected X.Y.Z or X.Y.Z-beta.N, for example 1.0.13 or 1.0.14-beta.1)"
  fi
  echo "v$raw"
}

# semver_key TAG: a string that sorts in release order. Zero-padded numbers, then
# 9 for a stable release or 1/2/3 for alpha/beta/rc, so 1.0.14-rc.1 < 1.0.14.
semver_key() {
  printf '%s\n' "${1#v}" | awk '{
    split($0, a, "-"); split(a[1], v, ".")
    rank = 9; n = 0
    if (a[2] != "") { split(a[2], p, "."); rank = (p[1] == "alpha") ? 1 : (p[1] == "beta") ? 2 : 3; n = p[2] }
    printf "%09d.%09d.%09d.%d.%09d\n", v[1], v[2], v[3], rank, n }'
}

# newest_of TAG...: the highest release among the tags given.
newest_of() {
  for candidate in "$@"; do printf '%s %s\n' "$(semver_key "$candidate")" "$candidate"; done | sort | tail -n 1 | awk '{print $2}'
}

latest_tag() {
  if [ -n "$BASE_OVERRIDE" ]; then
    fetch_or_die "$BASE_OVERRIDE/LATEST" "$TMP/latest"
    tr -d '[:space:]' < "$TMP/latest"
    return
  fi
  # The redirect target of /releases/latest names the tag and, unlike the API,
  # is not rate limited.
  final=""
  if [ "$DL" = curl ]; then
    final="$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" 2>/dev/null || true)"
  else
    final="$(wget -q --server-response --spider --max-redirect=0 "https://github.com/$REPO/releases/latest" 2>&1 | awk 'tolower($1)=="location:"{print $2}' | tr -d '\r' | tail -n 1 || true)"
  fi
  case "$final" in
    */releases/tag/*) echo "${final##*/}"; return ;;
  esac
  debug "redirect lookup failed; trying the releases API"
  fetch_or_die "https://api.github.com/repos/$REPO/releases/latest" "$TMP/latest.json"
  tag="$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TMP/latest.json" | head -n 1)"
  [ -n "$tag" ] || die 4 "could not determine the latest Wizard release. Pass --version X.Y.Z."
  echo "$tag"
}

# published_prereleases FILE: tags of the published pre-releases in a GitHub
# releases list, one per line. Only releases GitHub itself flags as pre-releases
# count, never a numerically higher tag from an older release line, and never a
# draft. It reads key/value pairs rather than lines, so pretty or compact JSON
# both work.
published_prereleases() {
  grep -oE '"(tag_name|draft|prerelease)"[[:space:]]*:[[:space:]]*("[^"]*"|true|false)' "$1" | awk '
    /"tag_name"/   { tag = $0; sub(/^[^:]*:[[:space:]]*"/, "", tag); sub(/"$/, "", tag); draft = 0; next }
    /"draft"/      { draft = ($0 ~ /true$/) ? 1 : 0; next }
    /"prerelease"/ { if ($0 ~ /true$/ && !draft && tag != "") print tag; next }' |
    awk '/^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-(alpha|beta|rc)\.[1-9][0-9]*$/'
}

# fetch_api URL DEST: like fetch, for api.github.com, with GITHUB_TOKEN when set
# (the anonymous limit is 60 requests an hour per address).
fetch_api() {
  if [ "$DL" = curl ]; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      curl -fSL --proto '=https' --tlsv1.2 --retry 3 -H "Authorization: Bearer $GITHUB_TOKEN" -o "$2" "$1"
    else
      curl -fSL --proto '=https' --tlsv1.2 --retry 3 -o "$2" "$1"
    fi
  elif [ -n "${GITHUB_TOKEN:-}" ]; then
    wget -q --tries=3 --header="Authorization: Bearer $GITHUB_TOKEN" -O "$2" "$1"
  else
    wget -q --tries=3 -O "$2" "$1"
  fi
}

# newest_tag: the pre-release channel. The stable release, unless a published
# pre-release is newer than it.
newest_tag() {
  stable="$(latest_tag)"
  candidates="$stable"
  if [ -n "$BASE_OVERRIDE" ]; then
    # A mirror may publish LATEST-PRERELEASE beside LATEST.
    if fetch "$BASE_OVERRIDE/LATEST-PRERELEASE" "$TMP/latest-pre" 2>/dev/null; then
      candidates="$candidates $(tr -d '[:space:]' < "$TMP/latest-pre")"
    fi
  else
    fetch_api "https://api.github.com/repos/$REPO/releases?per_page=30" "$TMP/releases.json" 2>"$TMP/fetch.err" || {
      [ "$VERBOSE" = 1 ] && cat "$TMP/fetch.err" >&2
      die 4 "could not list Wizard pre-releases from GitHub (a rate limit is the usual cause).
       Set GITHUB_TOKEN (any token, no scopes) and re-run, or name one:  --version 1.0.14-beta.1"
    }
    candidates="$candidates $(published_prereleases "$TMP/releases.json" | tr '\n' ' ')"
  fi
  # shellcheck disable=SC2086  # word splitting of the tag list is the point
  newest_of $candidates
}

if [ -n "$VERSION_ARG" ]; then
  TAG="$(normalize_tag "$VERSION_ARG")"
elif [ "$CHANNEL" = pre-release ]; then
  step "Finding the newest release, pre-releases included"
  LATEST="$(newest_tag)"
  TAG="$(normalize_tag "$LATEST")"
else
  step "Finding the latest release"
  # Two steps so a network failure inside latest_tag ends the script with its
  # own message and exit code instead of being reported as a bad version.
  LATEST="$(latest_tag)"
  TAG="$(normalize_tag "$LATEST")"
fi

PKG="Wizard-$TAG-$OS-$ARCH"
ASSET="$PKG.zip"
if [ -n "$BASE_OVERRIDE" ]; then BASE_URL="$BASE_OVERRIDE/$TAG"; else BASE_URL="https://github.com/$REPO/releases/download/$TAG"; fi

# ---- existing installations --------------------------------------------------

BIN_DIR="$INSTALL_DIR/bin"
PREVIOUS=""
if [ -x "$INSTALL_DIR/current/cli/wizard" ]; then
  PREVIOUS="$("$INSTALL_DIR/current/cli/wizard" --version 2>/dev/null | awk '{print $3}' | tr -d ',' || true)"
fi

# Another Wizard on PATH (typically Homebrew's) would shadow, or be shadowed by,
# this one, and two installs update independently. Say so instead of creating
# a version conflict silently.
EXISTING="$(command -v wizard 2>/dev/null || true)"
if [ -n "$EXISTING" ] && [ "$FORCE" = 0 ]; then
  # Compare physical paths: /tmp is /private/tmp on macOS, and a symlinked
  # home would otherwise make our own install look like a foreign one.
  physical() { (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
  resolved="$(physical "$(dirname "$EXISTING")")/$(basename "$EXISTING")"
  install_physical="$(physical "$INSTALL_DIR")"
  case "$resolved" in
    "$install_physical"/*|"$INSTALL_DIR"/*) ;;
    *) die 1 "another Wizard is already installed at $EXISTING.
       Upgrade that one with its own tool (for Homebrew: brew upgrade wizard),
       remove it first, or pass --force to install here as well." ;;
  esac
fi

# ---- download and verify -----------------------------------------------------

step "Installing Wizard $TAG for $TARGET"
[ -z "$PREVIOUS" ] || say "    (replacing $PREVIOUS)"

fetch_or_die "$BASE_URL/SHA256SUMS" "$TMP/SHA256SUMS"
step "Downloading $ASSET"
fetch_or_die "$BASE_URL/$ASSET" "$TMP/$ASSET"

# The digest comes from the release's own SHA256SUMS. Names are compared with
# any "./" or "*" prefix removed (older releases wrote "./name").
EXPECTED="$(awk -v n="$ASSET" '{ f=$NF; sub(/^\*/, "", f); sub(/^\.\//, "", f); if (f == n) print tolower($1) }' "$TMP/SHA256SUMS")"
[ -n "$EXPECTED" ] || die 1 "$ASSET is not listed in this release's SHA256SUMS; refusing to install it"
# Exactly one 64-digit hex entry: a duplicate adds a newline, which fails this.
case "$EXPECTED" in
  *[!0-9a-f]*) die 1 "SHA256SUMS has a malformed or duplicate entry for $ASSET; refusing to install it" ;;
esac
[ "$(printf '%s' "$EXPECTED" | wc -c | tr -d ' ')" = 64 ] || die 1 "SHA256SUMS has a malformed entry for $ASSET; refusing to install it"
ACTUAL="$(sha256_of "$TMP/$ASSET")"
if [ "$ACTUAL" != "$EXPECTED" ]; then
  die 1 "checksum mismatch for $ASSET
       expected $EXPECTED
       got      $ACTUAL
       The download is corrupt or has been tampered with. Nothing was installed."
fi
ok "checksum verified"

# ---- unpack into a staging directory beside the destination ------------------

mkdir -p "$INSTALL_DIR" 2>/dev/null || die 1 "cannot create $INSTALL_DIR (permission denied?). Choose another location with --install-dir."
[ -w "$INSTALL_DIR" ] || die 1 "$INSTALL_DIR is not writable. Choose another location with --install-dir."
STAGE="$(mktemp -d "$INSTALL_DIR/.wizard-update-XXXXXX")" || die 1 "could not create a staging directory in $INSTALL_DIR"

extract() {
  archive=$1 destination=$2
  # Validate before extraction. In particular, neither unzip nor bsdtar is a
  # suitable fallback on its own: a ZIP symlink can make a later member escape
  # the staging directory even when every member name looks harmless.
  if command -v unzip >/dev/null 2>&1 && command -v zipinfo >/dev/null 2>&1; then
    unzip -Z1 "$archive" > "$TMP/archive.paths" || die 1 "could not read $ASSET as a ZIP archive"
    if grep -E '(^[/\\]|(^|[/\\])\.\.([/\\]|$)|^[A-Za-z]:|\\)' "$TMP/archive.paths" >/dev/null 2>&1; then
      die 1 "the archive contains an unsafe path; refusing to unpack it"
    fi
    zipinfo -l "$archive" > "$TMP/archive.info" || die 1 "could not inspect $ASSET"
    if awk '/^l/ { found=1 } END { exit(found ? 0 : 1) }' "$TMP/archive.info"; then
      die 1 "the archive contains a symbolic link; refusing to unpack it"
    fi
    unzip -q -o "$archive" -d "$destination"
  elif command -v python3 >/dev/null 2>&1; then
    # Python's ZipInfo exposes Unix mode bits, so this fallback applies the
    # same path and symlink rules before it calls extractall.
    python3 - "$archive" "$destination" <<'PY'
import posixpath
import stat
import sys
import zipfile

archive, destination = sys.argv[1:]
with zipfile.ZipFile(archive) as zf:
    for info in zf.infolist():
        name = info.filename.replace('\\', '/')
        normalized = posixpath.normpath(name)
        mode = info.external_attr >> 16
        if (name.startswith('/') or normalized == '..' or normalized.startswith('../')
                or (len(name) >= 2 and name[1] == ':')
                or stat.S_IFMT(mode) == stat.S_IFLNK):
            raise SystemExit('unsafe ZIP member: ' + info.filename)
    zf.extractall(destination)
PY
  else
    die 3 "cannot unpack a verified .zip: install unzip and zipinfo (or python3), then re-run."
  fi
  # Defence in depth for ZIP tools whose metadata reporting differs by host.
  if find "$destination" -type l | head -n 1 | grep -q .; then
    die 1 "the archive produced a symbolic link; refusing to install it"
  fi
}
step "Unpacking"
extract "$TMP/$ASSET" "$STAGE" || die 1 "could not unpack $ASSET"

[ -f "$STAGE/$PKG/backend/main.py" ] && [ -f "$STAGE/$PKG/frontend/package.json" ] && [ -f "$STAGE/$PKG/cli/wizard" ] \
  || die 1 "the archive is not a complete Wizard package (missing backend, frontend or the CLI)"
chmod +x "$STAGE/$PKG/cli/wizard"

# Run the new binary before anything is switched over: a wrong-architecture or
# blocked binary fails here, with the old installation untouched.
REPORTED="$("$STAGE/$PKG/cli/wizard" --version 2>&1)" || die 1 "the downloaded program does not run on this machine: $REPORTED"
case "$REPORTED" in
  *"$TAG"*) ;;
  *) die 1 "the downloaded program reports '$REPORTED' but $TAG was requested" ;;
esac

# ---- switch over -------------------------------------------------------------

if [ -d "$INSTALL_DIR/$PKG" ]; then
  say "    $TAG is already unpacked in $INSTALL_DIR; keeping it"
else
  # Carry the existing configuration forward before the new package takes over.
  if [ -f "$INSTALL_DIR/current/backend/.env" ] && [ ! -f "$STAGE/$PKG/backend/.env" ]; then
    cp "$INSTALL_DIR/current/backend/.env" "$STAGE/$PKG/backend/.env" && chmod 600 "$STAGE/$PKG/backend/.env"
    debug "kept backend/.env from the previous release"
  fi
  mv "$STAGE/$PKG" "$INSTALL_DIR/$PKG"
fi

mkdir -p "$BIN_DIR"
ln -sfn "$PKG" "$INSTALL_DIR/current"
ln -sfn "../current/cli/wizard" "$BIN_DIR/wizard"

# ---- PATH --------------------------------------------------------------------

# One small file does the PATH work; startup files only source it, so re-running
# the installer never stacks lines and `wizard uninstall` removes one block.
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
ENV_FILE="$INSTALL_DIR/env"
{
  printf '%s\n' "# Wizard shell setup. Generated by the installer; removed by 'wizard uninstall'."
  printf 'case ":${PATH}:" in\n  *:%s:*) ;;\n  *) export PATH=%s ;;\nesac\n' "\"$BIN_DIR\"" "\"$BIN_DIR:\$PATH\""
} > "$ENV_FILE"

# add_block FILE LINE: put a marked block into FILE, replacing one that is there.
add_block() {
  file=$1; line=$2
  mkdir -p "$(dirname "$file")"
  if [ -f "$file" ]; then
    # Drop the block and the blank line(s) put in front of it, so re-running the
    # installer leaves the file byte-for-byte as it was.
    awk -v s="$RC_START" -v e="$RC_END" '
      $0==s { skip=1; blank=0; next }
      skip && $0==e { skip=0; next }
      skip { next }
      $0=="" { blank++; next }
      { while (blank>0) { print ""; blank-- } print }
      END { while (blank>0) { print ""; blank-- } }' "$file" > "$TMP/rc.tmp"
  else
    : > "$TMP/rc.tmp"
  fi
  { cat "$TMP/rc.tmp"; printf '\n%s\n%s\n%s\n' "$RC_START" "$line" "$RC_END"; } > "$TMP/rc.new"
  cat "$TMP/rc.new" > "$file"   # in place, so a symlinked dotfile stays a symlink
  say "    updated $file"
}

on_path() { case ":$PATH:" in *":$BIN_DIR:"*) return 0 ;; esac; return 1; }
MODIFIED=""
if [ "$NO_MODIFY_PATH" = 1 ]; then
  debug "leaving shell startup files alone (--no-modify-path)"
elif on_path; then
  debug "$BIN_DIR is already on PATH"
else
  step "Adding wizard to your PATH"
  SHELL_NAME="${SHELL:-}"; SHELL_NAME="${SHELL_NAME##*/}"   # SHELL is unset in many containers
  SOURCE_LINE=". $(quote "$ENV_FILE")"
  touched=0
  # zsh (the macOS default): ~/.zshrc, created if it does not exist, because a
  # fresh Mac has none and editing "the first file that exists" left a
  # working install with no `wizard` command in new terminals.
  zrc="${ZDOTDIR:-$HOME}/.zshrc"
  if [ "$SHELL_NAME" = zsh ] || [ -f "$zrc" ]; then add_block "$zrc" "$SOURCE_LINE"; touched=1; fi
  # bash: interactive shells read .bashrc on Linux; macOS Terminal starts login
  # shells, which read .bash_profile (else .profile).
  if [ -f "$HOME/.bashrc" ]; then add_block "$HOME/.bashrc" "$SOURCE_LINE"; touched=1; fi
  for login in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
    if [ -f "$login" ]; then
      case "$login" in "$HOME/.profile") [ "$SHELL_NAME" = bash ] || [ "$SHELL_NAME" = sh ] || [ "$SHELL_NAME" = dash ] || [ "$SHELL_NAME" = ash ] || continue ;; esac
      add_block "$login" "$SOURCE_LINE"; touched=1; break
    fi
  done
  if [ "$SHELL_NAME" = bash ] && [ ! -f "$HOME/.bashrc" ] && [ ! -f "$HOME/.bash_profile" ] && [ ! -f "$HOME/.bash_login" ] && [ ! -f "$HOME/.profile" ]; then
    if [ "$OS" = darwin ]; then add_block "$HOME/.bash_profile" "$SOURCE_LINE"; else add_block "$HOME/.bashrc" "$SOURCE_LINE"; fi
    touched=1
  fi
  # sh, dash, ash and unknown shells read ~/.profile.
  case "$SHELL_NAME" in
    sh|dash|ash|ksh) [ -f "$HOME/.profile" ] || { add_block "$HOME/.profile" "$SOURCE_LINE"; touched=1; } ;;
  esac
  # fish loads conf.d drop-ins; this file is ours alone.
  if [ "$SHELL_NAME" = fish ] || [ -d "$HOME/.config/fish" ]; then
    add_block "$HOME/.config/fish/conf.d/wizard.fish" "fish_add_path -g $(quote "$BIN_DIR")"
    touched=1
  fi
  [ "$touched" = 1 ] || add_block "$HOME/.profile" "$SOURCE_LINE"
  MODIFIED=1
fi

# ---- verify ------------------------------------------------------------------

INSTALLED="$("$BIN_DIR/wizard" --version 2>&1)" || die 1 "the installed program failed to run: $INSTALLED"
ok "$INSTALLED"

# Remember an explicit channel choice, so `wizard update` keeps following it. A
# pre-release build with no choice made stays on the pre-release channel by
# itself; only a choice needs writing down.
if [ -n "$CHANNEL" ]; then
  if "$BIN_DIR/wizard" channel "$CHANNEL" >/dev/null 2>&1; then
    ok "update channel: $CHANNEL"
  else
    warn "could not save the update channel. Run this once:  wizard channel $CHANNEL"
  fi
fi

say ""
say "Wizard $TAG is installed in $INSTALL_DIR"
case "$TAG" in
  *-*)
    say "This is a pre-release. To follow stable releases only, run:  wizard channel stable"
    say "(you stay on this build until a stable release passes it)"
    ;;
esac
if ! on_path; then
  if [ "$NO_MODIFY_PATH" = 1 ]; then
    say "PATH was not modified. To use wizard, add this directory to it:"
    say "    $BIN_DIR"
  else
    say "Open a new terminal, or run this to use it now:"
    say "    . \"$ENV_FILE\""
  fi
fi
say ""
say "Next:"
say "    wizard init      set up a provider and install dependencies"
say "    wizard start     launch Wizard"
say "    wizard doctor    check this installation"
