#!/bin/sh
# Tests for scripts/install.sh, run against a local fake release server.
#
#   sh scripts/tests/test_install_sh.sh            # run under every shell found: sh, dash, bash
#   SHELLS="dash" sh scripts/tests/test_install_sh.sh
#
# Each case gets its own HOME, install directory and a PATH made only of the
# tools the installer needs, so nothing on the machine running the tests (a
# Homebrew wizard, curl configs, dotfiles) can change the result.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALLER="$HERE/../install.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/wizard-install-test.XXXXXX")"
PASS=0; FAIL=0
SERVER_PID=""

cleanup() { [ -z "$SERVER_PID" ] || kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# ---- a toolbox PATH containing only what the installer may use ---------------

TOOLS="$WORK/tools"; mkdir -p "$TOOLS"
for tool in awk sed tr cat mktemp mv cp ln rm mkdir rmdir chmod dirname basename wc grep sort head tail date unzip zip zipinfo find \
            curl wget sha256sum shasum openssl sysctl python3 env id ls touch cut expr uname; do
  path="$(command -v "$tool" 2>/dev/null || true)"
  case "$path" in /*) ln -sf "$path" "$TOOLS/$tool" ;; esac
done
REAL_UNAME="$(command -v uname)"

# ---- a fake release ----------------------------------------------------------

case "$($REAL_UNAME -s)" in Darwin) OS=darwin ;; *) OS=linux ;; esac
case "$($REAL_UNAME -m)" in x86_64|amd64) ARCH=amd64 ;; *) ARCH=arm64 ;; esac
SERVER_ROOT="$WORK/server"; mkdir -p "$SERVER_ROOT"

# make_release TAG [reported-version]: builds <TAG>/Wizard-<TAG>-<os>-<arch>.zip and SHA256SUMS
make_release() {
  tag=$1; reported=${2:-$1}
  pkg="Wizard-$tag-$OS-$ARCH"
  build="$WORK/build-$tag"; rm -rf "$build"; mkdir -p "$build/$pkg/backend" "$build/$pkg/frontend" "$build/$pkg/cli"
  : > "$build/$pkg/backend/main.py"; echo '{}' > "$build/$pkg/frontend/package.json"
  cat > "$build/$pkg/cli/wizard" <<EOF
#!/bin/sh
case "\$1" in
  --version|version) echo "wizard CLI $reported, backend API compat v4.0.0" ;;
  --help) echo "Usage: wizard" ;;
  channel) [ ! -f "\$HOME/.fail-channel" ] || exit 1; printf '%s\n' "\$2" > "\$HOME/.channel-called" ;;
  *) echo "fake wizard" ;;
esac
EOF
  chmod +x "$build/$pkg/cli/wizard"
  rm -rf "$SERVER_ROOT/$tag"; mkdir -p "$SERVER_ROOT/$tag"   # fresh: zip would otherwise append to an old archive
  (cd "$build" && zip -qr "$SERVER_ROOT/$tag/$pkg.zip" "$pkg")
  (cd "$SERVER_ROOT/$tag" && if command -v sha256sum >/dev/null 2>&1; then sha256sum "$pkg.zip"; else shasum -a 256 "$pkg.zip"; fi > SHA256SUMS)
  # A mirror names the newest stable release in LATEST and, optionally, the
  # newest pre-release in LATEST-PRERELEASE. A pre-release never moves LATEST.
  case "$tag" in
    *-*) echo "$tag" > "$SERVER_ROOT/LATEST-PRERELEASE" ;;
    *)   echo "$tag" > "$SERVER_ROOT/LATEST" ;;
  esac
}

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
make_release v9.9.9
( cd "$SERVER_ROOT" && exec python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 ) &
SERVER_PID=$!
i=0; until curl -fsS "http://127.0.0.1:$PORT/LATEST" >/dev/null 2>&1; do i=$((i+1)); [ $i -lt 50 ] || { echo "test server did not start"; exit 1; }; sleep 0.1; done
BASE="http://127.0.0.1:$PORT"

# ---- harness -----------------------------------------------------------------

CASE=0
new_case() {
  CASE=$((CASE+1))
  HOME_DIR="$WORK/case$CASE/home"; INSTALL="$HOME_DIR/.wizard"; mkdir -p "$HOME_DIR"
  STUBS="$WORK/case$CASE/stubs"; mkdir -p "$STUBS"
  OUT="$WORK/case$CASE/out"
}

# run_installer [args...]: runs the installer under $UNDER with a scrubbed environment.
run_installer() {
  ( cd "$HOME_DIR" && env -i HOME="$HOME_DIR" PATH="$STUBS:$TOOLS" SHELL="${TEST_SHELL:-/bin/zsh}" TERM=dumb \
      WIZARD_RELEASE_BASE_URL="${TEST_BASE:-$BASE}" ${EXTRA_ENV:-} "$UNDER" "$INSTALLER" "$@" ) > "$OUT" 2>&1
}

t_pass() { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
t_fail() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$1"; printf '%s\n' "------ installer output ------"; sed 's/^/  | /' "$OUT" 2>/dev/null || true; printf '%s\n' "------------------------------"; }
expect() { # expect DESCRIPTION command...
  desc=$1; shift
  if "$@" >/dev/null 2>&1; then t_pass "$desc"; else t_fail "$desc"; fi
}
exit_is() { # exit_is CODE DESCRIPTION args...
  want=$1; desc=$2; shift 2
  set +e; run_installer "$@"; got=$?; set -e
  if [ "$got" = "$want" ]; then t_pass "$desc (exit $got)"; else t_fail "$desc (exit $got, wanted $want)"; fi
}
count_blocks() { grep -c '>>> wizard (managed by the Wizard installer) >>>' "$1" 2>/dev/null || true; }

# ---- the cases ---------------------------------------------------------------

run_suite() {
  UNDER=$1
  printf '\n== installer under %s ==\n' "$UNDER"
  rm -f "$SERVER_ROOT/LATEST-PRERELEASE"
  make_release v9.9.9   # every pass starts from the same server state ("latest" is 9.9.9)

  new_case; TEST_SHELL=/bin/zsh
  exit_is 0 "fresh install of the latest release" --yes
  expect "current points at the versioned package" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.9-$OS-$ARCH"
  expect "bin/wizard runs and reports the tag" sh -c "\"$INSTALL/bin/wizard\" --version | grep -q v9.9.9"
  expect "the launcher is a relative symlink through current" test "$(readlink "$INSTALL/bin/wizard")" = "../current/cli/wizard"
  expect "a zsh user with NO ~/.zshrc still gets PATH set up (fresh macOS)" grep -q '.wizard/env' "$HOME_DIR/.zshrc"
  expect "no staging directory is left behind" sh -c "! ls -d $INSTALL/.wizard-update-* >/dev/null 2>&1"
  cp "$HOME_DIR/.zshrc" "$WORK/zshrc.after-first-run"
  exit_is 0 "running the installer again succeeds" --yes
  expect "re-running does not duplicate the PATH block" test "$(count_blocks "$HOME_DIR/.zshrc")" = 1
  expect "re-running leaves the startup file byte-for-byte unchanged (no growing blank lines)" cmp -s "$HOME_DIR/.zshrc" "$WORK/zshrc.after-first-run"
  expect "the env file exports the bin directory once" sh -c "grep -c 'export PATH' '$INSTALL/env' | grep -qx 1"

  new_case; TEST_SHELL=/bin/bash
  printf 'export EDITOR=vim\n' > "$HOME_DIR/.bashrc"; printf 'echo login\n' > "$HOME_DIR/.bash_profile"
  exit_is 0 "bash install into existing startup files" --version 9.9.9
  expect ".bashrc keeps the user's line and gains one block" sh -c "grep -q 'EDITOR=vim' '$HOME_DIR/.bashrc' && test \$(grep -c '>>> wizard' '$HOME_DIR/.bashrc') = 1"
  expect "the macOS login file (.bash_profile) is covered too" grep -q '.wizard/env' "$HOME_DIR/.bash_profile"

  new_case
  exit_is 0 "--no-modify-path" --no-modify-path
  expect "no startup file is created or edited" sh -c "! ls '$HOME_DIR'/.zshrc '$HOME_DIR'/.bashrc '$HOME_DIR'/.profile '$HOME_DIR'/.bash_profile >/dev/null 2>&1"
  expect "the install is still usable by full path" sh -c "'$INSTALL/bin/wizard' --version | grep -q v9.9.9"

  new_case; TEST_SHELL=/usr/bin/fish; mkdir -p "$HOME_DIR/.config/fish"
  exit_is 0 "fish user" --yes
  expect "a conf.d drop-in is written (config.fish untouched)" grep -q 'fish_add_path' "$HOME_DIR/.config/fish/conf.d/wizard.fish"

  new_case
  exit_is 0 "install directory with spaces" --install-dir "$HOME_DIR/my wizard dir"
  expect "it runs from a path with spaces" sh -c "'$HOME_DIR/my wizard dir/bin/wizard' --version | grep -q v9.9.9"
  expect "the env file quotes the path" grep -q 'my wizard dir/bin' "$HOME_DIR/my wizard dir/env"

  # -- integrity --------------------------------------------------------------
  new_case
  make_release v9.9.8; cp "$SERVER_ROOT/v9.9.8/SHA256SUMS" "$WORK/sums-good"
  printf 'corrupted' >> "$SERVER_ROOT/v9.9.8/Wizard-v9.9.8-$OS-$ARCH.zip"   # SUMS now describes different bytes
  exit_is 1 "a corrupted archive is refused" --version 9.9.8
  expect "the mismatch is reported" grep -qi 'checksum mismatch' "$OUT"
  expect "nothing was installed" sh -c "! test -e '$INSTALL/current' && ! test -e '$INSTALL/Wizard-v9.9.8-$OS-$ARCH'"

  new_case
  make_release v9.9.7; : > "$SERVER_ROOT/v9.9.7/SHA256SUMS"
  exit_is 1 "an archive missing from SHA256SUMS is refused" --version 9.9.7
  expect "nothing was installed" sh -c "! test -e '$INSTALL/current'"

  new_case
  make_release v9.9.6; sed 's/  / .\//; s/ \./  \./' "$SERVER_ROOT/v9.9.6/SHA256SUMS" > "$WORK/dotslash"; cp "$WORK/dotslash" "$SERVER_ROOT/v9.9.6/SHA256SUMS"
  exit_is 0 "a SHA256SUMS written as './name' (releases through v1.0.12) is accepted" --version 9.9.6

  new_case
  make_release v9.9.5 v0.0.1     # the binary inside claims another version
  exit_is 1 "a binary that reports the wrong version is refused" --version 9.9.5
  expect "the old install (none here) was not switched" sh -c "! test -e '$INSTALL/current'"

  new_case
  make_release v9.9.4
  python3 - "$SERVER_ROOT/v9.9.4/Wizard-v9.9.4-$OS-$ARCH.zip" <<'PY'
import sys
import zipfile
with zipfile.ZipFile(sys.argv[1], 'a') as zf:
    zf.writestr('../../outside-wizard-stage', 'unsafe')
PY
  (cd "$SERVER_ROOT/v9.9.4" && if command -v sha256sum >/dev/null 2>&1; then sha256sum "Wizard-v9.9.4-$OS-$ARCH.zip"; else shasum -a 256 "Wizard-v9.9.4-$OS-$ARCH.zip"; fi > SHA256SUMS)
  exit_is 1 "an archive with a traversal member is refused before extraction" --version 9.9.4
  expect "an unsafe archive leaves no current package" sh -c "! test -e '$INSTALL/current'"

  new_case
  make_release v9.9.3
  python3 - "$SERVER_ROOT/v9.9.3/Wizard-v9.9.3-$OS-$ARCH.zip" <<'PY'
import stat
import sys
import zipfile
with zipfile.ZipFile(sys.argv[1], 'a') as zf:
    info = zipfile.ZipInfo('link')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    zf.writestr(info, '/tmp/unsafe')
PY
  (cd "$SERVER_ROOT/v9.9.3" && if command -v sha256sum >/dev/null 2>&1; then sha256sum "Wizard-v9.9.3-$OS-$ARCH.zip"; else shasum -a 256 "Wizard-v9.9.3-$OS-$ARCH.zip"; fi > SHA256SUMS)
  exit_is 1 "an archive with a symbolic link is refused before extraction" --version 9.9.3

  # -- platform ---------------------------------------------------------------
  new_case
  printf '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo riscv64;; *) echo Linux;; esac\n' > "$STUBS/uname"; chmod +x "$STUBS/uname"
  exit_is 3 "an unsupported CPU architecture fails cleanly" --yes
  expect "it names the architecture and the supported list" sh -c "grep -q riscv64 '$OUT' && grep -q 'Supported:' '$OUT'"

  new_case
  printf '#!/bin/sh\ncase "$1" in -s) echo FreeBSD;; -m) echo amd64;; *) echo FreeBSD;; esac\n' > "$STUBS/uname"; chmod +x "$STUBS/uname"
  exit_is 3 "an unsupported operating system fails cleanly" --yes

  new_case
  printf '#!/bin/sh\ncase "$1" in -s) echo MINGW64_NT-10.0;; -m) echo x86_64;; *) echo x;; esac\n' > "$STUBS/uname"; chmod +x "$STUBS/uname"
  exit_is 3 "Git Bash on Windows is pointed at the PowerShell installer" --yes
  expect "the message names install.ps1" grep -q 'install.ps1' "$OUT"

  # -- usage and environment --------------------------------------------------
  new_case
  exit_is 2 "an unknown option is a usage error" --frobnicate
  exit_is 2 "a malformed version is a usage error" --version 'v1.0.13; rm -rf /'
  exit_is 2 "a version that is not X.Y.Z is a usage error" --version 1.0
  exit_is 2 "installing into HOME itself is refused" --install-dir "$HOME_DIR"
  exit_is 2 "an install path with shell metacharacters is refused" --install-dir "$HOME_DIR/a\$b"
  exit_is 0 "--help works without a network" --help

  new_case; TEST_BASE="http://127.0.0.1:1"
  exit_is 4 "an unreachable server is a network error" --version 9.9.9
  expect "it suggests the proxy variables" grep -q 'HTTPS_PROXY' "$OUT"
  TEST_BASE=""

  # -- upgrades and coexistence ----------------------------------------------
  new_case
  exit_is 0 "install 9.9.9" --version 9.9.9
  mkdir -p "$INSTALL/current/backend" && printf 'GEMINI_API_KEY=keep-me\n' > "$INSTALL/current/backend/.env"
  make_release v9.9.10
  exit_is 0 "upgrade to 9.9.10" --version 9.9.10
  expect "current moved to the new release" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.10-$OS-$ARCH"
  expect "the previous release is kept for rollback" test -d "$INSTALL/Wizard-v9.9.9-$OS-$ARCH"
  expect "the user's backend/.env came along" grep -q 'keep-me' "$INSTALL/current/backend/.env"

  new_case
  mkdir -p "$STUBS"; printf '#!/bin/sh\necho other wizard\n' > "$STUBS/wizard"; chmod +x "$STUBS/wizard"
  exit_is 1 "another Wizard on PATH (for example Homebrew's) is not silently shadowed" --yes
  expect "the message says how to resolve it" grep -q 'force' "$OUT"
  expect "nothing was installed" sh -c "! test -e '$INSTALL/current'"
  exit_is 0 "--force installs anyway" --force

  new_case
  exit_is 0 "install with SHELL unset (a container)" --yes

  pre_release_cases
}

# ---- pre-releases and channels -------------------------------------------------

check_eq() { # check_eq DESCRIPTION GOT WANT
  printf '%s\n' "$2" > "$OUT"
  if [ "$2" = "$3" ]; then t_pass "$1"; else printf '%s\n' "$2" > "$OUT"; t_fail "$1 (wanted: $3)"; fi
}

# A realistic slice of GitHub's releases list: nested objects, a draft, an
# unflagged candidate, a tag outside the grammar, and the older v2.x line that
# outranks v1.0.x numerically but is not a pre-release.
write_releases_fixture() { # write_releases_fixture PRETTY COMPACT
  python3 - "$1" "$2" <<'PY'
import json
import sys


def rel(tag, pre=False, draft=False):
    return {"url": "https://api.github.com/x", "id": 1, "author": {"login": "bot", "type": "User"},
            "tag_name": tag, "target_commitish": "master", "name": tag, "draft": draft, "prerelease": pre,
            "assets": [{"name": "SHA256SUMS", "uploader": {"login": "bot"}, "size": 1}]}


releases = [
    rel("v1.0.14-beta.2", pre=True), rel("v1.0.14-beta.10", pre=True), rel("v1.0.13"),
    rel("v1.0.14-rc.1"),
    rel("v9.9.9-beta.1", pre=True, draft=True),
    rel("nightly", pre=True),
    rel("v2.2.1"), rel("v2.0.0-w2-planning"),
    rel("v1.0.14-alpha.1", pre=True),
]
open(sys.argv[1], "w").write(json.dumps(releases, indent=2))
open(sys.argv[2], "w").write(json.dumps(releases))
PY
}

# A curl that plays GitHub: the /releases/latest redirect, the releases API, and
# the release downloads, all served from $SERVER_ROOT. Anything else fails loudly.
write_curl_stub() {
  cat > "$STUBS/curl" <<'STUB'
#!/bin/sh
out=""; fmt=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w) fmt=$2; shift 2 ;;
    --proto|--retry|--retry-delay|-H) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
echo "$url" >> "$STUB_LOG"
case "$url" in
  https://github.com/Wizard-AIA/Wizard-w2/releases/latest)
    [ -z "$fmt" ] || printf 'https://github.com/Wizard-AIA/Wizard-w2/releases/tag/%s' "$(cat "$SERVER_ROOT/LATEST")" ;;
  https://api.github.com/repos/Wizard-AIA/Wizard-w2/releases?per_page=30)
    [ -f "$FAKE_RELEASES_JSON" ] || exit 22
    cp "$FAKE_RELEASES_JSON" "$out" ;;
  https://github.com/Wizard-AIA/Wizard-w2/releases/download/*)
    cp "$SERVER_ROOT/${url#https://github.com/Wizard-AIA/Wizard-w2/releases/download/}" "$out" ;;
  *) echo "unexpected URL: $url" >&2; exit 22 ;;
esac
STUB
  chmod +x "$STUBS/curl"
}

pre_release_cases() {
  # -- the release grammar ----------------------------------------------------
  for bad in 9.9.9-beta 9.9.9-beta.0 9.9.9-beta.01 9.9.9-preview.1 9.9.9-Beta.1 9.9.9-beta.1.2 9.9.9-beta.1+x 9.9.9+x 09.9.9 9.9.9.1 9.9 latest; do
    new_case; exit_is 2 "--version $bad is not a release version" --version "$bad"
  done
  new_case; exit_is 2 "an unknown channel is a usage error" --channel beta
  new_case; exit_is 2 "--channel needs a value" --channel

  # -- against a mirror (LATEST, and optionally LATEST-PRERELEASE) -------------
  make_release v9.9.9
  make_release v9.9.10-beta.1   # LATEST stays v9.9.9; LATEST-PRERELEASE names the beta

  new_case
  exit_is 0 "a default install ignores a newer pre-release" --yes
  expect "it installed the stable release" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.9-$OS-$ARCH"
  expect "no channel was written, none was chosen" sh -c "! test -e '$HOME_DIR/.channel-called'"

  new_case
  exit_is 0 "--pre-release installs a pre-release that is newer than stable" --pre-release
  expect "current is the beta" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.10-beta.1-$OS-$ARCH"
  expect "bin/wizard reports the pre-release" sh -c "'$INSTALL/bin/wizard' --version | grep -q v9.9.10-beta.1"
  expect "the pre-release channel was saved" test "$(cat "$HOME_DIR/.channel-called")" = pre-release
  expect "the closing note says it is a pre-release and how to leave" sh -c "grep -q 'pre-release' '$OUT' && grep -q 'wizard channel stable' '$OUT'"

  new_case; EXTRA_ENV="WIZARD_CHANNEL=pre-release"
  exit_is 0 "WIZARD_CHANNEL=pre-release does the same" --yes
  expect "it installed the beta" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.10-beta.1-$OS-$ARCH"
  EXTRA_ENV=""

  new_case
  exit_is 0 "--channel pre-release is the same as --pre-release" --channel pre-release
  expect "it installed the beta" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.10-beta.1-$OS-$ARCH"

  new_case
  exit_is 0 "--version names a pre-release directly" --version 9.9.10-beta.1
  expect "it installed exactly that pre-release" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.10-beta.1-$OS-$ARCH"
  expect "naming a version chooses no channel" sh -c "! test -e '$HOME_DIR/.channel-called'"

  new_case
  exit_is 0 "--channel stable with a pre-release version keeps the stable choice" --channel stable --version 9.9.10-beta.1
  expect "the stable channel was saved" test "$(cat "$HOME_DIR/.channel-called")" = stable

  # A pre-release older than the stable release is not offered.
  make_release v9.9.9
  make_release v9.9.8-beta.1
  new_case
  exit_is 0 "--pre-release with only an older pre-release installs the stable release" --pre-release
  expect "it installed the stable release" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.9-$OS-$ARCH"
  expect "the channel choice is still saved" test "$(cat "$HOME_DIR/.channel-called")" = pre-release

  # A pre-release of the same version ranks below its stable release.
  make_release v9.9.9-rc.3
  new_case
  exit_is 0 "a release candidate of the current stable version does not replace it" --pre-release
  expect "it installed the stable release" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.9-$OS-$ARCH"

  # A mirror with no pre-release at all.
  rm -f "$SERVER_ROOT/LATEST-PRERELEASE"
  new_case
  exit_is 0 "--pre-release against a mirror without pre-releases installs stable" --pre-release
  expect "it installed the stable release" test "$(readlink "$INSTALL/current")" = "Wizard-v9.9.9-$OS-$ARCH"

  # Saving the channel is best effort: the install already worked.
  make_release v9.9.10-beta.1
  new_case; : > "$HOME_DIR/.fail-channel"
  exit_is 0 "a channel that cannot be saved does not fail the install" --pre-release
  expect "it says how to set it by hand" grep -q 'wizard channel pre-release' "$OUT"

  # -- the pure functions ------------------------------------------------------
  new_case
  sed -n '/^semver_key() {/,/^}/p; /^newest_of() {/,/^}/p; /^published_prereleases() {/,/^}/p' "$INSTALLER" > "$WORK/fns.sh"
  expect "the helper functions were found in the installer" test "$(grep -c '() {' "$WORK/fns.sh")" = 3
  fn() { env -i PATH="$TOOLS" "$UNDER" -c ". '$WORK/fns.sh'; $*"; }
  check_eq "beta.10 outranks beta.2 (numeric, not text)"   "$(fn newest_of v1.0.13 v1.0.14-beta.2 v1.0.14-beta.10)" v1.0.14-beta.10
  check_eq "a stable release outranks its own candidates"  "$(fn newest_of v1.0.14-rc.9 v1.0.14 v1.0.14-beta.1)" v1.0.14
  check_eq "rc outranks beta outranks alpha"               "$(fn newest_of v1.0.14-beta.9 v1.0.14-rc.1 v1.0.14-alpha.20)" v1.0.14-rc.1
  check_eq "1.0.10 outranks 1.0.9 (numeric, not text)"     "$(fn newest_of v1.0.9 v1.0.10)" v1.0.10
  check_eq "a pre-release of a later version outranks stable" "$(fn newest_of v1.0.13 v1.0.14-alpha.1)" v1.0.14-alpha.1
  check_eq "a later stable outranks an earlier pre-release" "$(fn newest_of v2.0.0 v1.9.9-rc.1)" v2.0.0

  write_releases_fixture "$WORK/releases-pretty.json" "$WORK/releases-compact.json"
  want='v1.0.14-beta.2
v1.0.14-beta.10
v1.0.14-alpha.1'
  check_eq "published pre-releases are read from pretty JSON (drafts, unflagged, odd tags and v2.x excluded)" "$(fn published_prereleases "$WORK/releases-pretty.json")" "$want"
  check_eq "and from compact single-line JSON" "$(fn published_prereleases "$WORK/releases-compact.json")" "$want"

  # -- the real GitHub code path, through a curl that plays GitHub --------------
  make_release v1.0.13
  make_release v1.0.14-beta.10
  github_env() { EXTRA_ENV="WIZARD_RELEASE_BASE_URL= SERVER_ROOT=$SERVER_ROOT STUB_LOG=$WORK/case$CASE/curl.log FAKE_RELEASES_JSON=$WORK/releases-pretty.json ${1:-}"; }

  new_case; write_curl_stub; github_env
  exit_is 0 "GitHub path: a default install resolves the stable release" --yes
  expect "it installed v1.0.13" test "$(readlink "$INSTALL/current")" = "Wizard-v1.0.13-$OS-$ARCH"
  expect "it never asked for the releases list" sh -c "! grep -q 'api.github.com' '$WORK/case$CASE/curl.log'"

  new_case; write_curl_stub; github_env
  exit_is 0 "GitHub path: --pre-release picks the newest flagged pre-release, not v2.2.1" --pre-release
  expect "it installed v1.0.14-beta.10" test "$(readlink "$INSTALL/current")" = "Wizard-v1.0.14-beta.10-$OS-$ARCH"
  expect "it asked for the releases list once" sh -c "test \$(grep -c 'api.github.com' '$WORK/case$CASE/curl.log') = 1"

  new_case; write_curl_stub; github_env "GITHUB_TOKEN=ghp_notarealtoken0000000000000000000000000"
  exit_is 0 "GitHub path: a GITHUB_TOKEN is accepted" --pre-release
  expect "the token is never printed" sh -c "! grep -q ghp_notarealtoken '$OUT'"

  new_case; write_curl_stub; github_env; rm -f "$WORK/releases-pretty.json"
  exit_is 4 "GitHub path: an unreachable releases list is a network error, not a silent stable install" --pre-release
  expect "it names GITHUB_TOKEN and --version" sh -c "grep -q GITHUB_TOKEN '$OUT' && grep -q -- '--version' '$OUT'"
  expect "nothing was installed" sh -c "! test -e '$INSTALL/current'"
  EXTRA_ENV=""
}

# ---- run ---------------------------------------------------------------------

SHELLS="${SHELLS:-sh dash bash}"
for shell in $SHELLS; do
  if [ "$shell" = sh ]; then path="/bin/sh"; else path="$(command -v "$shell" 2>/dev/null || true)"; fi
  [ -x "${path:-}" ] || { printf '\n== %s not found; skipped ==\n' "$shell"; continue; }
  run_suite "$path"
done

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
