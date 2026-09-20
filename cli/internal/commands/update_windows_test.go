//go:build windows

package commands

import (
	"os"
	"path/filepath"
	"testing"
)

// Runs on windows-latest in CI: the only place the mklink quoting bug (paths
// with spaces reached cmd.exe backslash-escaped) can actually be verified.
func TestSwapCurrentJunctionRepointsAndSurvivesSpacesInPaths(t *testing.T) {
	root := filepath.Join(t.TempDir(), "Wizard Install") // a space on purpose
	oldPkg := filepath.Join(root, "Wizard-v1.0.12-windows-amd64")
	newPkg := filepath.Join(root, "Wizard-v1.0.13-windows-amd64")
	for _, dir := range []string{oldPkg, newPkg} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "marker.txt"), []byte(filepath.Base(dir)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	current := filepath.Join(root, "current")
	if err := createJunction(current, oldPkg); err != nil {
		t.Fatalf("createJunction: %v", err)
	}
	if data, err := os.ReadFile(filepath.Join(current, "marker.txt")); err != nil || string(data) != filepath.Base(oldPkg) {
		t.Fatalf("junction does not resolve to the old package: %q, %v", data, err)
	}

	if err := swapCurrentJunction(current, newPkg); err != nil {
		t.Fatalf("swapCurrentJunction: %v", err)
	}
	if data, err := os.ReadFile(filepath.Join(current, "marker.txt")); err != nil || string(data) != filepath.Base(newPkg) {
		t.Fatalf("current does not resolve to the new package: %q, %v", data, err)
	}
	// Replacing the junction must never delete the packages behind it.
	for _, pkg := range []string{oldPkg, newPkg} {
		if _, err := os.Stat(filepath.Join(pkg, "marker.txt")); err != nil {
			t.Fatalf("package %s lost its files: %v", pkg, err)
		}
	}
	for _, leftover := range []string{current + ".next", current + ".prev"} {
		if _, err := os.Lstat(leftover); err == nil {
			t.Errorf("%s was left behind", leftover)
		}
	}
}

func TestSwapCurrentJunctionRollsBackWhenTargetIsInvalid(t *testing.T) {
	root := t.TempDir()
	pkg := filepath.Join(root, "pkg")
	if err := os.MkdirAll(pkg, 0o755); err != nil {
		t.Fatal(err)
	}
	current := filepath.Join(root, "current")
	if err := createJunction(current, pkg); err != nil {
		t.Fatal(err)
	}
	if err := swapCurrentJunction(current, filepath.Join(root, "does", "not", "exist")); err == nil {
		t.Fatal("expected an error for a missing target")
	}
	if _, err := os.Stat(current); err != nil {
		t.Fatal("a failed swap must leave the previous current junction in place")
	}
}

func TestCreateJunctionRejectsShellMetacharacters(t *testing.T) {
	if err := createJunction(`C:\a&b`, `C:\c`); err == nil {
		t.Fatal("paths with cmd metacharacters must be refused, not interpolated")
	}
}
