package installkind

import (
	"os"
	"path/filepath"
	"testing"
)

func mkfile(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
}

func TestDetectHomebrewKeg(t *testing.T) {
	keg := filepath.Join(t.TempDir(), "Cellar", "wizard", "1.0.13")
	mkfile(t, filepath.Join(keg, "libexec", "cli", "wizard"))
	got := Detect(filepath.Join(keg, "libexec", "cli", "wizard"), filepath.Join(keg, "libexec"))
	if got.Kind != Homebrew || !got.Kind.PackageManaged() {
		t.Fatalf("kind = %q, want Homebrew (package managed)", got.Kind)
	}
	if got.Kind.UpgradeCommand() == "" || got.Kind.UninstallCommand() == "" {
		t.Fatal("Homebrew must name upgrade and uninstall commands")
	}
}

func TestDetectScoopIsCaseInsensitive(t *testing.T) {
	root := filepath.Join(t.TempDir(), "Scoop", "Apps", "wizard", "current")
	mkfile(t, filepath.Join(root, "cli", "wizard.exe"))
	if got := Detect(filepath.Join(root, "cli", "wizard.exe"), root); got.Kind != Scoop {
		t.Fatalf("kind = %q, want Scoop", got.Kind)
	}
}

func TestDetectCheckout(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := Detect(filepath.Join(root, "cli", "wizard"), root); got.Kind != Checkout {
		t.Fatalf("kind = %q, want git checkout", got.Kind)
	}
}

func TestDetectDirectInstall(t *testing.T) {
	install := t.TempDir()
	pkg := filepath.Join(install, "Wizard-v1.0.13-test")
	mkfile(t, filepath.Join(pkg, "cli", launcherName()))
	mkfile(t, filepath.Join(install, "bin", launcherName()))
	if err := os.Symlink(filepath.Base(pkg), filepath.Join(install, "current")); err != nil {
		t.Skipf("symlinks unavailable here: %v", err)
	}
	got := Detect(filepath.Join(install, "bin", launcherName()), filepath.Join(install, "current"))
	if got.Kind != Direct {
		t.Fatalf("kind = %q, want release installer", got.Kind)
	}
	want, _ := filepath.EvalSymlinks(install)
	if got.InstallRoot != want {
		t.Fatalf("InstallRoot = %q, want %q", got.InstallRoot, want)
	}
}

// An extracted archive whose parent has no current pointer must not be
// mistaken for a managed install: update/uninstall would rename or delete the
// wrong directory.
func TestDetectPlainExtractedArchiveIsUnmanaged(t *testing.T) {
	root := filepath.Join(t.TempDir(), "Wizard-v1.0.13-linux-amd64")
	mkfile(t, filepath.Join(root, "cli", "wizard"))
	if got := Detect(filepath.Join(root, "cli", "wizard"), root); got.Kind != Unknown {
		t.Fatalf("kind = %q, want unmanaged", got.Kind)
	}
	if _, err := ManagedInstallRoot(root); err == nil {
		t.Fatal("ManagedInstallRoot accepted an unmanaged directory")
	}
}

func TestContainsRun(t *testing.T) {
	if !hasSegments("/opt/homebrew/Cellar/wizard/1.0.13", "Cellar", "wizard") {
		t.Error("expected match")
	}
	if hasSegments("/opt/Cellar/other/wizard", "Cellar", "wizard") {
		t.Error("segments must be consecutive")
	}
	if hasSegments(`C:\Users\x\Cellarwizard`, "Cellar", "wizard") {
		t.Error("must not match substrings")
	}
}

// The working directory must not decide who owns the running binary: an
// installed Wizard started from inside a git checkout is still an installed
// Wizard, and its Root is its own package, not that checkout.
func TestDetectPrefersTheBinarysOwnPackageOverTheWorkingDirectoryCheckout(t *testing.T) {
	install := t.TempDir()
	pkg := filepath.Join(install, "Wizard-v1.0.13-test")
	mkfile(t, filepath.Join(pkg, "cli", launcherName()))
	mkfile(t, filepath.Join(pkg, "backend", "main.py"))
	mkfile(t, filepath.Join(pkg, "frontend", "package.json"))
	mkfile(t, filepath.Join(install, "bin", launcherName()))
	if err := os.Symlink(filepath.Base(pkg), filepath.Join(install, "current")); err != nil {
		t.Skipf("symlinks unavailable here: %v", err)
	}
	checkout := t.TempDir()
	if err := os.Mkdir(filepath.Join(checkout, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	got := Detect(filepath.Join(install, "bin", launcherName()), checkout)
	if got.Kind != Direct {
		t.Fatalf("kind = %q, want release installer (the checkout is only the working directory)", got.Kind)
	}
	wantPkg, _ := filepath.EvalSymlinks(pkg)
	if got.Root != wantPkg {
		t.Fatalf("Root = %q, want the installed package %q", got.Root, wantPkg)
	}
}

func TestDetectHomebrewKegWinsOverAnUnrelatedCheckoutRoot(t *testing.T) {
	keg := filepath.Join(t.TempDir(), "Cellar", "wizard", "1.0.13", "libexec")
	mkfile(t, filepath.Join(keg, "cli", "wizard"))
	mkfile(t, filepath.Join(keg, "backend", "main.py"))
	mkfile(t, filepath.Join(keg, "frontend", "package.json"))
	checkout := t.TempDir()
	if err := os.Mkdir(filepath.Join(checkout, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	got := Detect(filepath.Join(keg, "cli", "wizard"), checkout)
	wantKeg, _ := filepath.EvalSymlinks(keg)
	if got.Kind != Homebrew || got.Root != wantKeg {
		t.Fatalf("got kind %q root %q, want Homebrew rooted at the keg %q", got.Kind, got.Root, wantKeg)
	}
}
