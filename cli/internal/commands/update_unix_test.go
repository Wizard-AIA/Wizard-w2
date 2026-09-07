//go:build !windows

package commands

import (
	"os"
	"path/filepath"
	"testing"
)

func TestActivateStagedReleaseAdvancesOnlyManagedPointers(t *testing.T) {
	installRoot := t.TempDir()
	oldPackage := filepath.Join(installRoot, "Wizard-v1.0.0-test")
	newStage := filepath.Join(installRoot, ".stage")
	newPackage := filepath.Join(newStage, "Wizard-v1.1.0-test")
	for _, root := range []string{oldPackage, newPackage} {
		if err := os.MkdirAll(filepath.Join(root, "cli"), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, "cli", "wizard"), []byte("binary"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Mkdir(filepath.Join(installRoot, "bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Base(oldPackage), filepath.Join(installRoot, "current")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join("..", "current", "cli", "wizard"), filepath.Join(installRoot, "bin", "wizard")); err != nil {
		t.Fatal(err)
	}
	pending, err := activateStagedRelease(installRoot, newStage, newPackage, "v1.1.0", false, "8000", "3000")
	if err != nil || pending {
		t.Fatalf("activateStagedRelease = (%v, %v), want (false, nil)", pending, err)
	}
	current, err := filepath.EvalSymlinks(filepath.Join(installRoot, "current"))
	wantCurrent, wantErr := filepath.EvalSymlinks(filepath.Join(installRoot, "Wizard-v1.1.0-test"))
	if err != nil || wantErr != nil || current != wantCurrent {
		t.Fatalf("current = %q, %v; want new package", current, err)
	}
	launcher, err := os.Readlink(filepath.Join(installRoot, "bin", "wizard"))
	if err != nil || launcher != filepath.Join("..", "current", "cli", "wizard") {
		t.Fatalf("launcher = %q, %v; want stable current pointer", launcher, err)
	}
	if _, err := os.Stat(filepath.Join(oldPackage, "cli", "wizard")); err != nil {
		t.Fatalf("old package was not retained: %v", err)
	}
}
