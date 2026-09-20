package platform

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type manifest struct {
	Artifact string `json:"artifact"`
	Targets  []struct {
		OS   string `json:"os"`
		Arch string `json:"arch"`
	} `json:"targets"`
}

func loadManifest(t *testing.T) manifest {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "packaging", "release.json"))
	if err != nil {
		t.Fatalf("reading release manifest: %v", err)
	}
	var m manifest
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

// The Go side and packaging/release.json describe the same release matrix. If
// this fails, a target was added to one and not the other.
func TestTargetsMatchReleaseManifest(t *testing.T) {
	m := loadManifest(t)
	if len(m.Targets) != len(Supported) {
		t.Fatalf("manifest has %d targets, platform.Supported has %d", len(m.Targets), len(Supported))
	}
	for i, want := range m.Targets {
		if got := Supported[i]; got.OS != want.OS || got.Arch != want.Arch {
			t.Errorf("target %d = %v, manifest says %s/%s", i, got, want.OS, want.Arch)
		}
	}
}

func TestArtifactNameMatchesManifestTemplate(t *testing.T) {
	m := loadManifest(t)
	for _, target := range Supported {
		want := strings.NewReplacer("{tag}", "v1.0.13", "{os}", target.OS, "{arch}", target.Arch).Replace(m.Artifact)
		if got := ArtifactName("v1.0.13", target); got != want {
			t.Errorf("ArtifactName(%v) = %q, manifest template gives %q", target, got, want)
		}
	}
}

func TestSupported(t *testing.T) {
	if !(Target{"darwin", "arm64"}).Supported() {
		t.Error("darwin/arm64 must be supported")
	}
	for _, unsupported := range []Target{{"windows", "arm64"}, {"linux", "386"}, {"freebsd", "amd64"}} {
		if unsupported.Supported() {
			t.Errorf("%v must not be reported as supported", unsupported)
		}
	}
}

func TestBinaryName(t *testing.T) {
	if BinaryName("windows") != "wizard.exe" || BinaryName("linux") != "wizard" || BinaryName("darwin") != "wizard" {
		t.Error("unexpected binary names")
	}
}
