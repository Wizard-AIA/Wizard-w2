package commands

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"wizard/internal/exitcode"
)

func TestPathListWithoutKeepsEverythingElseIntact(t *testing.T) {
	raw := `C:\Windows;%USERPROFILE%\bin;"C:\Users\Me\AppData\Local\Wizard\bin\";C:\tools;;`
	got, changed := pathListWithout(raw, `c:\users\me\appdata\local\wizard\bin`)
	if !changed {
		t.Fatal("expected the entry to be removed (case, quotes and trailing slash must not matter)")
	}
	if got != `C:\Windows;%USERPROFILE%\bin;C:\tools;;` {
		t.Fatalf("got %q; other entries, %%VARS%% and empty segments must be untouched", got)
	}
	if _, changed := pathListWithout(`C:\Windows;C:\tools`, `C:\Wizard\bin`); changed {
		t.Fatal("nothing to remove, must report unchanged")
	}
	if got, _ := pathListWithout(`C:\Wizard\bin2;C:\Wizard\bin`, `C:\Wizard\bin`); got != `C:\Wizard\bin2` {
		t.Fatalf("a prefix-sharing entry must survive, got %q", got)
	}
}

// managedLayout builds the directory tree the official installer creates.
func managedLayout(t *testing.T) (installRoot string, env *Env, out *bytes.Buffer) {
	t.Helper()
	base := t.TempDir()
	installRoot = filepath.Join(base, ".wizard")
	pkg := filepath.Join(installRoot, "Wizard-v1.0.13-test")
	for _, f := range []string{"backend/main.py", "frontend/package.json", "cli/wizard", "backend/.env"} {
		p := filepath.Join(pkg, filepath.FromSlash(f))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("KEY=secret\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(installRoot, "bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(installRoot, "bin", launcherFile()), []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Base(pkg), filepath.Join(installRoot, "current")); err != nil {
		t.Skipf("symlinks unavailable here: %v", err)
	}
	// A file the user keeps in the install directory must survive.
	if err := os.WriteFile(filepath.Join(installRoot, "my-notes.txt"), []byte("mine"), 0o644); err != nil {
		t.Fatal(err)
	}

	t.Setenv("HOME", base)
	t.Setenv("USERPROFILE", base)
	cfg := filepath.Join(base, "cfg")
	if err := os.MkdirAll(filepath.Join(cfg, "venv"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("WIZARD_CONFIG_DIR", cfg)
	current := filepath.Join(installRoot, "current")
	old := executablePath
	executablePath = func() (string, error) { return filepath.Join(installRoot, "bin", launcherFile()), nil }
	t.Cleanup(func() { executablePath = old })

	out = &bytes.Buffer{}
	env = &Env{
		RepoRoot: current, BackendDir: filepath.Join(current, "backend"), FrontendDir: filepath.Join(current, "frontend"),
		ConfigDir: cfg, RunDir: filepath.Join(cfg, "run"), LogsDir: filepath.Join(cfg, "logs"), VenvDir: filepath.Join(cfg, "venv"),
		Out: out, Err: out, In: strings.NewReader(""),
	}
	return installRoot, env, out
}

func isWindowsGOOS() bool { return runtime.GOOS == "windows" }

func launcherFile() string {
	if isWindowsGOOS() {
		return "wizard.exe"
	}
	return "wizard"
}

func TestUninstallRequiresConfirmationWhenNotInteractive(t *testing.T) {
	root, env, out := managedLayout(t)
	if code := RunUninstall(env, nil); code != exitcode.Usage {
		t.Fatalf("exit = %d, want usage error\n%s", code, out)
	}
	if _, err := os.Stat(root); err != nil {
		t.Fatal("nothing may be removed without confirmation")
	}
}

func TestUninstallRemovesProgramKeepsDataAndBacksUpEnv(t *testing.T) {
	root, env, out := managedLayout(t)
	if code := RunUninstall(env, []string{"--yes"}); code != exitcode.OK {
		t.Fatalf("exit = %d\n%s", code, out)
	}
	for _, gone := range []string{"bin", "current", "Wizard-v1.0.13-test"} {
		if _, err := os.Lstat(filepath.Join(root, gone)); err == nil {
			t.Errorf("%s should have been removed", gone)
		}
	}
	if data, err := os.ReadFile(filepath.Join(root, "my-notes.txt")); err != nil || string(data) != "mine" {
		t.Fatal("a file the user keeps in the install directory must never be deleted")
	}
	if _, err := os.Stat(filepath.Join(env.ConfigDir, "venv")); err != nil {
		t.Fatal("user data must be kept without --purge")
	}
	backup, err := os.ReadFile(filepath.Join(env.ConfigDir, "backend.env.backup"))
	if err != nil || !strings.Contains(string(backup), "KEY=secret") {
		t.Fatalf("API keys must be backed up before the package is deleted: %v", err)
	}
	if info, _ := os.Stat(filepath.Join(env.ConfigDir, "backend.env.backup")); info != nil && !isWindowsGOOS() && info.Mode().Perm() != 0o600 {
		t.Fatalf("backup mode = %v, want 0600", info.Mode().Perm())
	}
	if !strings.Contains(out.String(), "Your data was kept") {
		t.Fatalf("should say how to remove the kept data:\n%s", out)
	}
}

func TestUninstallPurgeRemovesTheWholeSystem(t *testing.T) {
	root, env, out := managedLayout(t)
	for _, alias := range []string{"--purge", "--all"} {
		root, env, out = managedLayout(t)
		if code := RunUninstall(env, []string{alias, "--yes"}); code != exitcode.OK {
			t.Fatalf("%s: exit = %d\n%s", alias, code, out)
		}
		if _, err := os.Stat(env.ConfigDir); err == nil {
			t.Errorf("%s: config directory (credentials, venv) must be deleted", alias)
		}
		if _, err := os.Stat(filepath.Join(root, "bin")); err == nil {
			t.Errorf("%s: program must be deleted", alias)
		}
		if _, err := os.Stat(filepath.Join(env.ConfigDir, "backend.env.backup")); err == nil {
			t.Errorf("%s: --purge must not leave a copy of the API keys behind", alias)
		}
	}
}

func TestUninstallNeverTouchesPackageManagerFiles(t *testing.T) {
	keg := filepath.Join(t.TempDir(), "Cellar", "wizard", "1.0.13", "libexec")
	if err := os.MkdirAll(filepath.Join(keg, "cli"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(keg, "cli", "wizard"), []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	old := executablePath
	executablePath = func() (string, error) { return filepath.Join(keg, "cli", "wizard"), nil }
	defer func() { executablePath = old }()
	var out bytes.Buffer
	env := &Env{RepoRoot: keg, ConfigDir: t.TempDir(), Out: &out, Err: &out, In: strings.NewReader("")}
	if code := RunUninstall(env, []string{"--yes"}); code != exitcode.Environment {
		t.Fatalf("exit = %d, want environment error\n%s", code, out.String())
	}
	if !strings.Contains(out.String(), "brew uninstall wizard") {
		t.Fatalf("must say how to remove a Homebrew install:\n%s", out.String())
	}
	if _, err := os.Stat(filepath.Join(keg, "cli", "wizard")); err != nil {
		t.Fatal("a Homebrew-managed file was deleted")
	}
}

func TestUninstallRefusesGitCheckoutAndUnknownLayouts(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	old := executablePath
	executablePath = func() (string, error) { return filepath.Join(root, "cli", "wizard"), nil }
	defer func() { executablePath = old }()
	var out bytes.Buffer
	env := &Env{RepoRoot: root, ConfigDir: t.TempDir(), Out: &out, Err: &out, In: strings.NewReader("")}
	if code := RunUninstall(env, []string{"--yes"}); code != exitcode.Environment {
		t.Fatalf("exit = %d\n%s", code, out.String())
	}
	if _, err := os.Stat(filepath.Join(root, ".git")); err != nil {
		t.Fatal("a git checkout must never be removed")
	}
}

func TestCheckSafeInstallRootRefusesDangerousRoots(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	for _, bad := range []string{string(filepath.Separator), home, filepath.Dir(home)} {
		if err := checkSafeInstallRoot(bad); err == nil {
			t.Errorf("%q must be refused", bad)
		}
	}
	if err := checkSafeInstallRoot(filepath.Join(home, ".wizard")); err != nil {
		t.Errorf("the default install root must be allowed: %v", err)
	}
}

func TestRemoveInstallTreeDoesNotFollowCurrentIntoThePackage(t *testing.T) {
	root, _, _ := managedLayout(t)
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "precious"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Repoint current at a directory outside the install.
	_ = os.Remove(filepath.Join(root, "current"))
	if err := os.Symlink(outside, filepath.Join(root, "current")); err != nil {
		t.Skip("symlinks unavailable")
	}
	if _, _, err := removeInstallTree(root); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(outside, "precious")); err != nil {
		t.Fatal("removing current followed the link and deleted the target's contents")
	}
}
