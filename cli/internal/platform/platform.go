// Package platform is the one place the CLI maps an OS/architecture pair to a
// release artifact. The authoritative list lives in packaging/release.json,
// which build_release.sh, the installers' tests and the packaging renderer all
// read; TestTargetsMatchReleaseManifest fails if this file drifts from it.
//
// The artifact naming is frozen for the 1.0.x line: CLIs already in the field
// look up "Wizard-<tag>-<goos>-<goarch>.zip" when they self-update.
package platform

import (
	"fmt"
	"runtime"
)

// Target is a GOOS/GOARCH pair, spelled the way Go and the release artifacts
// spell them (amd64, not x64).
type Target struct {
	OS   string
	Arch string
}

// Supported lists every target the release pipeline builds and smoke-tests.
var Supported = []Target{
	{"darwin", "arm64"},
	{"darwin", "amd64"},
	{"linux", "amd64"},
	{"linux", "arm64"},
	{"windows", "amd64"},
}

// Current is the target this binary was built for.
func Current() Target { return Target{OS: runtime.GOOS, Arch: runtime.GOARCH} }

func (t Target) String() string { return t.OS + "-" + t.Arch }

// Supported reports whether releases are published for t.
func (t Target) Supported() bool {
	for _, s := range Supported {
		if s == t {
			return true
		}
	}
	return false
}

// PackageName is the archive's top-level directory, e.g.
// "Wizard-v1.0.13-darwin-arm64".
func PackageName(tag string, t Target) string {
	return fmt.Sprintf("Wizard-%s-%s-%s", tag, t.OS, t.Arch)
}

// ArtifactName is the release archive for t, e.g.
// "Wizard-v1.0.13-darwin-arm64.zip".
func ArtifactName(tag string, t Target) string { return PackageName(tag, t) + ".zip" }

// BinaryName is the launcher's file name on goos.
func BinaryName(goos string) string {
	if goos == "windows" {
		return "wizard.exe"
	}
	return "wizard"
}

// UnsupportedMessage explains, for an actionable error, what to do on a
// platform no release is built for.
func UnsupportedMessage(t Target) string {
	return fmt.Sprintf("Wizard does not publish releases for %s. Supported platforms: %s.\n"+
		"Build from source instead: https://github.com/Wizard-AIA/Wizard-w2#contributing--development", t, list(Supported))
}

func list(ts []Target) string {
	out := ""
	for i, t := range ts {
		if i > 0 {
			out += ", "
		}
		out += t.String()
	}
	return out
}
