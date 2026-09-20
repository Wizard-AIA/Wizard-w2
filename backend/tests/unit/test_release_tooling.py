"""Tests for scripts/release.py and the single-source-of-truth version.

The release tool exists because v1.0.12 shipped a Homebrew formula whose SHA-256
values came from a local build, not from the archives CI published; `brew install`
failed with "Formula reports different checksum". These pin the fix: package
metadata is rendered from the published SHA256SUMS and from nothing else.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _load_release_tool():
    spec = importlib.util.spec_from_file_location("release_tool", ROOT / "scripts" / "release.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


release = _load_release_tool()
MANIFEST = release.load_manifest()

# The real SHA256SUMS published with v1.0.12 (note the "./" prefixes).
PUBLISHED_V1012 = """\
63ce9b896c128949c8ccc35a455cb04ad2875fc8a713fc2e55a40ddf410675b9  ./Wizard-v1.0.12-darwin-amd64.zip
3d1bee90b685e205476d515577fdb3d9b0a4fa0d5c6250223001f12d50678e47  ./Wizard-v1.0.12-darwin-arm64.zip
bf2c6a2b54d692a368af49e2e3e4b993721b64bf40581e073849af51d6c1a979  ./Wizard-v1.0.12-linux-amd64.zip
080aeee38cdbae2f884a2566b755e1237ef83b2cfaad19ca8293c591f8665fa8  ./Wizard-v1.0.12-linux-arm64.zip
1eb60393f79ec2d718ca96d5916c302ebd9c823be40d11461d69fccfc235a4ee  ./Wizard-v1.0.12-windows-amd64.zip
"""
# The hash the v1.0.12 formula pinned for darwin-arm64: from a local dist/ build.
LOCAL_BUILD_HASH = "00c9c873a4b2be401dd70d8549bd809b53384e74ae6feb4660e4734c75ef8491"


class TestVersionSingleSourceOfTruth:
    def test_backend_reports_the_version_file(self):
        from src.version import APP_VERSION

        assert APP_VERSION == (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)

    def test_api_schemas_default_to_that_version(self):
        from src.api.schemas import HealthResponse
        from src.version import APP_VERSION

        assert HealthResponse(version="4.0.0", sandbox_available=False, model_provider="x").app_version == APP_VERSION

    def test_every_copy_of_the_version_agrees(self):
        """The CI gate: frontend/package.json, CITATION.cff, generated types, installer target lists."""
        assert release.collect_problems() == []

    def test_a_tag_that_disagrees_with_version_is_refused(self):
        problems = release.collect_problems(tag="v0.0.1")
        assert any("does not match VERSION" in p for p in problems)

    def test_version_falls_back_to_the_environment_without_the_file(self, monkeypatch, tmp_path):
        import importlib

        import src.version as version_module

        monkeypatch.setenv("WIZARD_VERSION", "v7.8.9")
        # Point the module at a directory with no VERSION file.
        monkeypatch.setattr(version_module, "__file__", str(tmp_path / "a" / "b" / "version.py"))
        assert version_module._read_version() == "7.8.9"
        importlib.reload(version_module)  # leave the module as it was found


class TestChecksums:
    def test_published_format_with_dot_slash_prefix_parses(self):
        sums = release.parse_sums(PUBLISHED_V1012)
        assert sums["Wizard-v1.0.12-darwin-arm64.zip"].startswith("3d1bee90")
        assert len(sums) == 5

    @pytest.mark.parametrize("line", ["deadbeef  file.zip", "x" * 64 + "  file.zip", "not a checksum line"])
    def test_malformed_lines_are_rejected(self, line):
        with pytest.raises(release.ReleaseError):
            release.parse_sums(line)

    def test_duplicate_entries_are_rejected(self):
        digest = "a" * 64
        with pytest.raises(release.ReleaseError):
            release.parse_sums(f"{digest}  a.zip\n{digest}  a.zip\n")


class TestRendering:
    @pytest.fixture()
    def rendered(self, tmp_path):
        sums = release.parse_sums(PUBLISHED_V1012)
        formula = release.render_formula(MANIFEST, "1.0.12", sums)
        scoop = release.render_scoop(MANIFEST, "1.0.12", sums)
        meta = release.render_release_json(MANIFEST, "1.0.12", sums, {})
        return formula, scoop, meta, sums

    def test_formula_pins_the_published_checksums_not_a_local_build(self, rendered):
        formula, _, _, sums = rendered
        for name, digest in sums.items():
            if "windows" not in name:
                assert digest in formula, f"{name}'s published checksum is missing from the formula"
        assert LOCAL_BUILD_HASH not in formula  # the exact value that broke `brew install`

    def test_formula_uses_homebrew_arch_blocks_and_libexec(self, rendered):
        formula, *_ = rendered
        assert "on_macos do" in formula and "on_linux do" in formula
        assert formula.count("on_arm do") == 2 and formula.count("on_intel do") == 2
        assert "Hardware::CPU" not in formula  # the style Homebrew's linter rejects
        # Release ZIPs have one versioned top-level directory. Installing that
        # directory itself makes bin/wizard point at a nonexistent
        # libexec/cli/wizard, which was the macOS Homebrew failure.
        assert 'package_dir = Dir["*"].find' in formula
        assert 'libexec.install Dir[File.join(package_dir, "*")]' in formula
        assert 'bin.install_symlink libexec/"cli/wizard"' in formula
        assert "windows" not in formula  # no Homebrew target for Windows

    def test_formula_test_block_checks_the_version_it_installs(self, rendered):
        formula, *_ = rendered
        assert 'assert_match "wizard CLI v#{version}"' in formula

    def test_formula_urls_follow_the_artifact_template(self, rendered):
        formula, *_ = rendered
        urls = re.findall(r'url "([^"]+)"', formula)
        assert len(urls) == 4
        for url in urls:
            assert url.startswith(f"https://github.com/{MANIFEST['repo']}/releases/download/v1.0.12/Wizard-v1.0.12-")
            assert url.endswith(".zip")

    def test_scoop_manifest_uses_the_published_windows_hash(self, rendered):
        _, scoop, _, sums = rendered
        assert scoop["architecture"]["64bit"]["hash"] == sums["Wizard-v1.0.12-windows-amd64.zip"]
        assert scoop["env_set"] == {"WIZARD_ROOT": "$dir"}
        assert scoop["autoupdate"]["architecture"]["64bit"]["hash"]["url"].endswith("/SHA256SUMS")

    def test_release_json_lists_every_target_with_its_checksum(self, rendered):
        _, _, meta, sums = rendered
        assert meta["version"] == "1.0.12" and meta["tag"] == "v1.0.12"
        assert {(a["os"], a["arch"]) for a in meta["assets"]} == {(t["os"], t["arch"]) for t in MANIFEST["targets"]}
        for asset in meta["assets"]:
            assert asset["sha256"] == sums[asset["name"]]

    def test_rendering_refuses_a_checksum_file_missing_a_target(self, tmp_path):
        partial = "\n".join(PUBLISHED_V1012.splitlines()[:3])
        sums_file = tmp_path / "SHA256SUMS"
        sums_file.write_text(partial, encoding="utf-8")
        rc = release.main(["render", "--version", "1.0.12", "--sums", str(sums_file), "--out", str(tmp_path / "out")])
        assert rc == 1

    def test_end_to_end_render_writes_three_files(self, tmp_path):
        sums_file = tmp_path / "SHA256SUMS"
        sums_file.write_text(PUBLISHED_V1012, encoding="utf-8")
        out = tmp_path / "out"
        assert release.main(["render", "--version", "1.0.12", "--sums", str(sums_file), "--out", str(out)]) == 0
        assert {p.name for p in out.iterdir()} == {"wizard.rb", "wizard.json", "release.json"}
        json.loads((out / "wizard.json").read_text(encoding="utf-8"))


class TestVerify:
    @staticmethod
    def _make_dist(directory: Path, tag: str, *, dot_slash: bool = False) -> None:
        lines = []
        for t in MANIFEST["targets"]:
            name = release.artifact_name(MANIFEST, tag, t["os"], t["arch"])
            data = f"archive for {name}".encode()
            (directory / name).write_bytes(data)
            prefix = "./" if dot_slash else ""
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {prefix}{name}")
        (directory / MANIFEST["checksums"]).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_matching_archives_pass(self, tmp_path):
        self._make_dist(tmp_path, "v9.9.9")
        assert release.main(["verify", "--dir", str(tmp_path), "--tag", "v9.9.9"]) == 0

    def test_a_tampered_archive_fails(self, tmp_path):
        self._make_dist(tmp_path, "v9.9.9")
        next(tmp_path.glob("*darwin-arm64.zip")).write_bytes(b"tampered")
        assert release.main(["verify", "--dir", str(tmp_path), "--tag", "v9.9.9"]) == 1

    def test_a_missing_archive_fails(self, tmp_path):
        self._make_dist(tmp_path, "v9.9.9")
        next(tmp_path.glob("*linux-arm64.zip")).unlink()
        assert release.main(["verify", "--dir", str(tmp_path), "--tag", "v9.9.9"]) == 1

    def test_dot_slash_entries_are_refused_at_publish_time(self, tmp_path):
        """Every CLI since v1.0.10 rejects "./name"; new releases must not write it."""
        self._make_dist(tmp_path, "v9.9.9", dot_slash=True)
        assert release.main(["verify", "--dir", str(tmp_path), "--tag", "v9.9.9"]) == 1

    def test_an_unexpected_extra_entry_fails(self, tmp_path):
        self._make_dist(tmp_path, "v9.9.9")
        with (tmp_path / "SHA256SUMS").open("a", encoding="utf-8") as fh:
            fh.write("a" * 64 + "  surprise.zip\n")
        assert release.main(["verify", "--dir", str(tmp_path), "--tag", "v9.9.9"]) == 1


class TestPreReleaseTags:
    """A pre-release is tagged vX.Y.Z-KIND.N; only stable releases feed package managers.

    The grammar is shared with the Go CLI (cli/internal/relver), install.sh and
    install.ps1; each has its own test for the same table.
    """

    GOOD = {
        "v1.0.14": ("1.0.14", None),
        "v1.0.14-beta.1": ("1.0.14", "beta.1"),
        "v1.0.14-alpha.3": ("1.0.14", "alpha.3"),
        "v10.20.30-rc.12": ("10.20.30", "rc.12"),
    }
    BAD = [
        "",
        "v",
        "1.0.14",
        "v1.0",
        "v1.0.14.1",
        "v01.0.14",
        "v1.0.014",
        "v1.0.14-",
        "v1.0.14-beta",
        "v1.0.14-beta.",
        "v1.0.14-beta.0",
        "v1.0.14-beta.01",
        "v1.0.14-beta.1.2",
        "v1.0.14-preview.1",
        "v1.0.14-Beta.1",
        "v1.0.14-beta.1+build",
        "v1.0.14+build",
        "v1.0.14-beta.x",
        "latest",
        "nightly",
        # `$` would accept a trailing newline and `\d` would accept other scripts'
        # digits; the Go, sh and PowerShell parsers accept neither.
        "v1.0.14\n",
        "v1.0.14-beta.1\n",
        "v1.0.1\u0664",
        "v1.0.14-beta.\u0661",
    ]

    @pytest.mark.parametrize("tag", list(GOOD))
    def test_valid_tags_parse(self, tag):
        base, pre = self.GOOD[tag]
        info = release.parse_tag(tag)
        assert (info.base, info.prerelease) == (base, pre)
        assert info.version == tag.removeprefix("v")

    @pytest.mark.parametrize("tag", BAD)
    def test_everything_else_is_refused(self, tag):
        with pytest.raises(release.ReleaseError):
            release.parse_tag(tag)

    def test_the_version_file_rejects_leading_zeros_like_the_cli_does(self):
        assert release.SEMVER.match("1.0.13")
        assert not release.SEMVER.match("1.0.013")
        assert not release.SEMVER.match("1.0.13-beta.1")  # VERSION is the base; the suffix lives on the tag
        assert not release.SEMVER.match("1.0.13\n")
        assert not release.SEMVER.match("1.0.1\u0664")

    def test_a_pre_release_of_the_current_version_is_consistent(self):
        version = release.read_version()
        assert release.collect_problems(tag=f"v{version}-beta.1") == []
        assert release.collect_problems(tag=f"v{version}-rc.2") == []

    def test_a_pre_release_of_another_version_is_refused(self):
        problems = release.collect_problems(tag="v0.0.1-beta.1")
        assert any("does not match VERSION" in p for p in problems)

    def test_a_malformed_tag_is_reported_not_crashed_on(self):
        assert any("not a release tag" in p for p in release.collect_problems(tag="v1.0.14-preview.1"))

    def test_only_a_stable_release_needs_a_changelog_section(self, monkeypatch):
        monkeypatch.setattr(release, "read_version", lambda: "9.9.9")
        stable = release.collect_problems(tag="v9.9.9", changelog=True)
        pre = release.collect_problems(tag="v9.9.9-beta.1", changelog=True)
        assert any("CHANGELOG.md has no" in p for p in stable)
        assert not any("CHANGELOG" in p for p in pre)

    def test_tag_info_prints_what_the_workflow_reads(self, capsys):
        assert release.main(["tag-info", "v1.0.14-beta.2"]) == 0
        lines = dict(line.split("=", 1) for line in capsys.readouterr().out.split())
        assert lines == {"tag": "v1.0.14-beta.2", "version": "1.0.14-beta.2", "base": "1.0.14", "prerelease": "true"}
        assert release.main(["tag-info", "v1.0.14"]) == 0
        assert "prerelease=false" in capsys.readouterr().out

    def test_tag_info_refuses_a_malformed_tag(self, capsys):
        assert release.main(["tag-info", "v1.0.14-beta"]) == 1
        assert "not a release tag" in capsys.readouterr().err

    def test_archives_of_a_pre_release_verify_under_their_own_names(self, tmp_path):
        TestVerify._make_dist(tmp_path, "v9.9.9-beta.1")
        assert (tmp_path / "Wizard-v9.9.9-beta.1-linux-amd64.zip").exists()
        assert release.main(["verify", "--dir", str(tmp_path), "--tag", "v9.9.9-beta.1"]) == 0

    def test_verify_refuses_a_malformed_tag(self, tmp_path):
        TestVerify._make_dist(tmp_path, "v9.9.9")
        with pytest.raises(release.ReleaseError):
            release.cmd_verify(release.argparse.Namespace(dir=str(tmp_path), tag="v9.9.9-preview"))

    def test_package_metadata_is_never_rendered_for_a_pre_release(self, tmp_path, capsys):
        sums_file = tmp_path / "SHA256SUMS"
        sums_file.write_text(PUBLISHED_V1012, encoding="utf-8")
        out = tmp_path / "out"
        rc = release.main(["render", "--version", "1.0.14-beta.1", "--sums", str(sums_file), "--out", str(out)])
        assert rc == 1
        assert "pre-release" in capsys.readouterr().err
        assert not out.exists(), "a pre-release must not leave a formula, manifest or release.json behind"
