#!/usr/bin/env python3
"""Release tooling: one version, one target matrix, packaging rendered from the published checksums.

Why this exists: the v1.0.12 Homebrew formula pinned SHA-256 values copied from a
maintainer's local build, not from the archives CI published, so `brew install`
failed with "Formula reports different checksum". Nothing here trusts a local
build. Package metadata is rendered from the SHA256SUMS that was actually
published with the release.

Sources of truth
  VERSION                  the release version (X.Y.Z, no "v")
  packaging/release.json   repository, artifact name template, target matrix

Commands
  check                    fail if any version or target list has drifted (CI gate)
  set-version X.Y.Z        update every copy of the version that must exist
  verify --dir DIR         check DIR/SHA256SUMS against the archives beside it
  render                   write wizard.rb, wizard.json and release.json from a SHA256SUMS
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
CHECKSUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(?:\./)?(\S.*)$")
# In frontend/lib/api-types.generated.ts: "App Version" then its "@default X.Y.Z".
APP_VERSION_DEFAULT = re.compile(r"(App Version\s*\n\s*\* @default )\d+\.\d+\.\d+")


class ReleaseError(Exception):
    """A problem the operator must fix; printed without a traceback."""


# --- sources of truth -------------------------------------------------------


def read_version() -> str:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not SEMVER.match(version):
        raise ReleaseError(f"VERSION must be X.Y.Z, got {version!r}")
    return version


def load_manifest() -> dict:
    return json.loads((ROOT / "packaging" / "release.json").read_text(encoding="utf-8"))


def artifact_name(manifest: dict, tag: str, os_name: str, arch: str) -> str:
    return manifest["artifact"].replace("{tag}", tag).replace("{os}", os_name).replace("{arch}", arch)


def release_url(manifest: dict, tag: str, name: str) -> str:
    return f"https://github.com/{manifest['repo']}/releases/download/{tag}/{name}"


# --- checksums --------------------------------------------------------------


def parse_sums(text: str) -> dict[str, str]:
    """Parse sha256sum output into {file name: digest}, tolerating "./name" and "*name"."""
    sums: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = CHECKSUM_LINE.match(line)
        if not match:
            raise ReleaseError(f"unrecognised SHA256SUMS line: {raw!r}")
        digest, name = match.group(1).lower(), match.group(2).strip()
        if name in sums:
            raise ReleaseError(f"duplicate SHA256SUMS entry for {name}")
        sums[name] = digest
    return sums


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_assets(manifest: dict, tag: str) -> list[tuple[str, str, str]]:
    return [(t["os"], t["arch"], artifact_name(manifest, tag, t["os"], t["arch"])) for t in manifest["targets"]]


# --- check ------------------------------------------------------------------


def _first_match(path: str, pattern: str, flags: int = 0) -> str | None:
    match = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), flags)
    return match.group(1) if match else None


def collect_problems(tag: str | None = None, changelog: bool = False) -> list[str]:
    problems: list[str] = []
    try:
        version = read_version()
    except (ReleaseError, OSError) as exc:
        return [str(exc)]

    copies = {
        "frontend/package.json": _first_match("frontend/package.json", r'"version":\s*"([^"]+)"'),
        "CITATION.cff": _first_match("CITATION.cff", r"^version:\s*(\S+)\s*$", re.MULTILINE),
    }
    for path, value in copies.items():
        if value != version:
            problems.append(
                f"{path} says {value!r} but VERSION is {version!r} (run: scripts/release.py set-version {version})"
            )

    types = ROOT / "frontend" / "lib" / "api-types.generated.ts"
    if types.exists():
        found = re.findall(r"App Version\s*\n\s*\* @default (\d+\.\d+\.\d+)", types.read_text(encoding="utf-8"))
        if not found or any(v != version for v in found):
            problems.append(
                f"frontend/lib/api-types.generated.ts app_version defaults {sorted(set(found))} do not match VERSION "
                f"(run: scripts/release.py set-version {version})"
            )

    if tag is not None and tag != f"v{version}":
        problems.append(f"tag {tag!r} does not match VERSION (expected v{version})")

    if changelog and f"## [v{version}]" not in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"):
        problems.append(f"CHANGELOG.md has no '## [v{version}]' section")

    manifest = load_manifest()

    def targets_for(windows: bool) -> str:
        return " ".join(f"{t['os']}-{t['arch']}" for t in manifest["targets"] if (t["os"] == "windows") == windows)

    for script, wanted in (("scripts/install.sh", targets_for(False)), ("scripts/install.ps1", targets_for(True))):
        path = ROOT / script
        if not path.exists():
            problems.append(f"{script} is missing")
            continue
        listed = _first_match(script, r"^#\s*wizard-targets:\s*(.+?)\s*$", re.MULTILINE)
        if listed != wanted:
            problems.append(f"{script} targets {listed!r} but packaging/release.json says {wanted!r}")
        text = path.read_text(encoding="utf-8")
        # A "v1.0.x" tag outside a comment is a fallback release, the way the old
        # installers defaulted to v1.0.12 when the GitHub lookup failed. (Bare
        # "1.0.13" in help text is only an example.)
        if re.search(r"\bv\d+\.\d+\.\d+\b", re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)):
            problems.append(f"{script} hardcodes a release tag; it must resolve the release at run time")

    for template in ("packaging/homebrew/wizard.rb.tmpl",):
        if not (ROOT / template).exists():
            problems.append(f"{template} is missing")
    return problems


def cmd_check(args: argparse.Namespace) -> int:
    problems = collect_problems(args.tag, args.changelog)
    if problems:
        print("release consistency check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"release consistency OK (version {read_version()})")
    return 0


# --- set-version ------------------------------------------------------------


def _sub_once(path: str, pattern: str, replacement: str, flags: int = 0) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    new, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise ReleaseError(f"could not find the version in {path}")
    file.write_text(new, encoding="utf-8")


def cmd_set_version(args: argparse.Namespace) -> int:
    new = args.version.removeprefix("v")
    if not SEMVER.match(new):
        raise ReleaseError(f"version must be X.Y.Z, got {args.version!r}")
    old = read_version()
    (ROOT / "VERSION").write_text(new + "\n", encoding="utf-8")
    _sub_once("frontend/package.json", r'("version":\s*")[^"]+(")', rf"\g<1>{new}\g<2>")
    _sub_once("CITATION.cff", r"^(version:\s*)\S+(\s*)$", rf"\g<1>{new}\g<2>", re.MULTILINE)
    # The generated TypeScript contract embeds the backend's default app_version.
    # The contract-drift workflow regenerates it and fails on any difference, so
    # it moves with the version. Matched by context (the "App Version" field),
    # not by the previous value, so it works whatever that was.
    types = ROOT / "frontend" / "lib" / "api-types.generated.ts"
    if types.exists():
        text = types.read_text(encoding="utf-8")
        types.write_text(re.sub(APP_VERSION_DEFAULT, rf"\g<1>{new}", text), encoding="utf-8")
    openapi = ROOT / "backend" / "openapi.json"  # generated, untracked; kept in step when present
    if openapi.exists():
        text = openapi.read_text(encoding="utf-8")
        openapi.write_text(
            re.sub(r'("app_version":\s*\{[^{}]*?"default":\s*")[^"]+(")', rf"\g<1>{new}\g<2>", text, flags=re.DOTALL),
            encoding="utf-8",
        )
    print(f"version {old} -> {new}")
    return 0


# --- verify -----------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    manifest = load_manifest()
    tag = args.tag or f"v{read_version()}"
    sums_path = directory / manifest["checksums"]
    if not sums_path.exists():
        raise ReleaseError(f"{sums_path} not found")
    text = sums_path.read_text(encoding="utf-8")
    if re.search(r"^[0-9a-fA-F]{64}\s+\*?\./", text, re.MULTILINE):
        raise ReleaseError(
            "SHA256SUMS uses './name' entries; releases through v1.0.12 did, and CLIs built from them reject that format. "
            "Write bare file names."
        )
    sums = parse_sums(text)
    problems = []
    for _os, _arch, name in expected_assets(manifest, tag):
        archive = directory / name
        if name not in sums:
            problems.append(f"{name} is not listed in SHA256SUMS")
        elif not archive.exists():
            problems.append(f"{name} is listed but the file is missing")
        elif sha256_file(archive) != sums[name]:
            problems.append(f"{name}: SHA-256 does not match SHA256SUMS")
    extra = sorted(set(sums) - {n for _, _, n in expected_assets(manifest, tag)})
    if extra:
        problems.append(f"SHA256SUMS lists unexpected files: {', '.join(extra)}")
    if problems:
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"all {len(sums)} archives match SHA256SUMS")
    return 0


# --- render -----------------------------------------------------------------


def _fetch(url: str) -> str:
    if not url.startswith("https://"):
        raise ReleaseError(f"refusing a non-HTTPS checksum URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "wizard-release-tool"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https only, checked above
        return response.read().decode("utf-8")


def _brew_blocks(manifest: dict, tag: str, sums: dict[str, str]) -> str:
    """Homebrew's on_macos/on_linux blocks, with on_arm/on_intel inside each."""
    blocks = []
    for os_name, brew_os in (("darwin", "on_macos"), ("linux", "on_linux")):
        lines = [f"  {brew_os} do"]
        for arch, cond in (("arm64", "on_arm"), ("amd64", "on_intel")):
            name = artifact_name(manifest, tag, os_name, arch)
            if not any(t["os"] == os_name and t["arch"] == arch for t in manifest["targets"]):
                continue
            lines += [
                f"    {cond} do",
                f'      url "{release_url(manifest, tag, name)}"',
                f'      sha256 "{sums[name]}"',
                "    end",
            ]
        lines.append("  end")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_formula(manifest: dict, version: str, sums: dict[str, str]) -> str:
    tag = f"v{version}"
    template = (ROOT / "packaging" / "homebrew" / "wizard.rb.tmpl").read_text(encoding="utf-8")
    return template.replace("@ASSETS@", _brew_blocks(manifest, tag, sums))


def render_scoop(manifest: dict, version: str, sums: dict[str, str]) -> dict:
    tag = f"v{version}"
    name = artifact_name(manifest, tag, "windows", "amd64")
    base = f"https://github.com/{manifest['repo']}/releases/download"
    pkg = artifact_name(manifest, "v$version", "windows", "amd64").removesuffix(".zip")
    return {
        "version": version,
        "description": "Local-first autonomous AI data analyst workspace",
        "homepage": "https://wizardw2.vercel.app",
        "license": "BSD-3-Clause",
        "architecture": {
            "64bit": {
                "url": release_url(manifest, tag, name),
                "hash": sums[name],
                "extract_dir": name.removesuffix(".zip"),
            }
        },
        "bin": "cli\\wizard.exe",
        # Pins the bundled backend/frontend location for the scoop layout, so it
        # never has to be discovered from the shim.
        "env_set": {"WIZARD_ROOT": "$dir"},
        "checkver": {"github": f"https://github.com/{manifest['repo']}"},
        "autoupdate": {
            "architecture": {
                "64bit": {
                    "url": f"{base}/v$version/{artifact_name(manifest, 'v$version', 'windows', 'amd64')}",
                    "extract_dir": pkg,
                    "hash": {"url": f"{base}/v$version/{manifest['checksums']}"},
                }
            }
        },
    }


def render_release_json(manifest: dict, version: str, sums: dict[str, str], sizes: dict[str, int]) -> dict:
    tag = f"v{version}"
    assets = []
    for os_name, arch, name in expected_assets(manifest, tag):
        asset = {
            "name": name,
            "os": os_name,
            "arch": arch,
            "url": release_url(manifest, tag, name),
            "sha256": sums[name],
        }
        if name in sizes:
            asset["size"] = sizes[name]
        assets.append(asset)
    return {
        "schema": 1,
        "version": version,
        "tag": tag,
        "repo": manifest["repo"],
        "checksums_url": release_url(manifest, tag, manifest["checksums"]),
        "assets": assets,
    }


def cmd_render(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    version = (args.version or read_version()).removeprefix("v")
    if not SEMVER.match(version):
        raise ReleaseError(f"version must be X.Y.Z, got {version!r}")
    tag = f"v{version}"

    if args.sums_url:
        text = _fetch(args.sums_url)
    elif args.sums:
        text = Path(args.sums).read_text(encoding="utf-8")
    else:
        text = _fetch(release_url(manifest, tag, manifest["checksums"]))
    sums = parse_sums(text)
    missing = [n for _, _, n in expected_assets(manifest, tag) if n not in sums]
    if missing:
        raise ReleaseError("SHA256SUMS is missing: " + ", ".join(missing))

    sizes: dict[str, int] = {}
    if args.dir:
        for _, _, name in expected_assets(manifest, tag):
            archive = Path(args.dir) / name
            if archive.exists():
                sizes[name] = archive.stat().st_size

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "wizard.rb").write_text(render_formula(manifest, version, sums), encoding="utf-8")
    (out / "wizard.json").write_text(
        json.dumps(render_scoop(manifest, version, sums), indent=2) + "\n", encoding="utf-8"
    )
    (out / "release.json").write_text(
        json.dumps(render_release_json(manifest, version, sums, sizes), indent=2) + "\n", encoding="utf-8"
    )
    print(f"rendered wizard.rb, wizard.json, release.json for {tag} into {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="fail if versions or target lists have drifted")
    p.add_argument("--tag", help="also require this git tag to equal v<VERSION>")
    p.add_argument("--changelog", action="store_true", help="also require a CHANGELOG section for this version")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("set-version", help="update every copy of the version")
    p.add_argument("version")
    p.set_defaults(func=cmd_set_version)

    p = sub.add_parser("verify", help="check SHA256SUMS against the archives beside it")
    p.add_argument("--dir", default=str(ROOT / "dist"))
    p.add_argument("--tag")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("render", help="render Homebrew, Scoop and release metadata from a SHA256SUMS")
    p.add_argument("--version", help="defaults to VERSION")
    p.add_argument("--sums", help="path to a SHA256SUMS file")
    p.add_argument("--sums-url", help="HTTPS URL of a SHA256SUMS file")
    p.add_argument("--dir", help="directory of archives, only to record their sizes")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_render)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
