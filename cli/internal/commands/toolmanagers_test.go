package commands

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func mkdirs(t *testing.T, paths ...string) {
	t.Helper()
	for _, p := range paths {
		if err := os.MkdirAll(p, 0o755); err != nil {
			t.Fatal(err)
		}
	}
}

// The machine this was found on had Node and pnpm installed through nvm but not
// on the PATH of the process running `wizard init`; init reported them missing.
func TestVersionManagerBinsFindsNvmNodeNewestFirst(t *testing.T) {
	home := t.TempDir()
	mkdirs(t,
		filepath.Join(home, ".nvm", "versions", "node", "v9.11.2", "bin"),
		filepath.Join(home, ".nvm", "versions", "node", "v22.11.0", "bin"),
		filepath.Join(home, ".nvm", "versions", "node", "v26.9.0", "bin"),
		filepath.Join(home, ".volta", "bin"),
	)
	got := versionManagerBins(home)
	want := []string{
		filepath.Join(home, ".nvm", "versions", "node", "v26.9.0", "bin"),
		filepath.Join(home, ".nvm", "versions", "node", "v22.11.0", "bin"),
		filepath.Join(home, ".nvm", "versions", "node", "v9.11.2", "bin"),
		filepath.Join(home, ".volta", "bin"),
	}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("position %d: got %s, want %s (numeric, not textual, version order: v9 < v22 < v26)", i, got[i], want[i])
		}
	}
}

func TestVersionManagerBinsOmitsDirectoriesThatDoNotExist(t *testing.T) {
	if got := versionManagerBins(t.TempDir()); len(got) != 0 {
		t.Fatalf("an empty home has no version managers, got %v", got)
	}
	if got := versionManagerBins(""); got != nil {
		t.Fatalf("no home, no result, got %v", got)
	}
}

func TestSortVersionDirsDescIsNumeric(t *testing.T) {
	p := []string{"/x/v9.1.0/bin", "/x/v20.5.1/bin", "/x/v20.10.0/bin", "/x/v3.0.0/bin"}
	sortVersionDirsDesc(p)
	want := []string{"/x/v20.10.0/bin", "/x/v20.5.1/bin", "/x/v9.1.0/bin", "/x/v3.0.0/bin"}
	for i := range want {
		if p[i] != want[i] {
			t.Fatalf("got %v, want %v", p, want)
		}
	}
}

// The PATH refresh must keep the user's own tools first: extra locations are a
// fallback, never an override.
func TestRefreshToolPathAppendsAndNeverReorders(t *testing.T) {
	home := t.TempDir()
	mkdirs(t, filepath.Join(home, ".nvm", "versions", "node", "v26.9.0", "bin"))
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	own := t.TempDir()
	t.Setenv("PATH", own)
	old := toolPathHook
	toolPathHook = versionManagerBins
	defer func() { toolPathHook = old }()

	refreshToolPath()
	entries := filepath.SplitList(os.Getenv("PATH"))
	if entries[0] != own {
		t.Fatalf("the user's PATH entry must stay first, got %v", entries)
	}
	if runtime.GOOS != "windows" {
		found := false
		for _, e := range entries[1:] {
			found = found || e == filepath.Join(home, ".nvm", "versions", "node", "v26.9.0", "bin")
		}
		if !found {
			t.Fatalf("nvm's Node was not appended: %v", entries)
		}
	}
}
