package commands

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testInstall = "/home/u/.wizard"

func TestStripRemovesMarkedBlockOnly(t *testing.T) {
	in := "export EDITOR=vim\n\n" + rcBlockStart + "\n. \"/home/u/.wizard/env\"\n" + rcBlockEnd + "\nalias ll='ls -l'\n"
	got, changed := stripWizardShellIntegration(in, testInstall)
	if !changed {
		t.Fatal("expected a change")
	}
	if strings.Contains(got, "wizard") || strings.Contains(got, ".wizard/env") {
		t.Fatalf("block not removed:\n%s", got)
	}
	if !strings.Contains(got, "export EDITOR=vim") || !strings.Contains(got, "alias ll='ls -l'") {
		t.Fatalf("user lines must survive:\n%s", got)
	}
}

// The v1.0.x installers appended these loose lines.
func TestStripRemovesLegacyInstallerLinesOnly(t *testing.T) {
	in := "export PATH=\"$HOME/bin:$PATH\"\n\n# Wizard CLI\nexport PATH=\"/home/u/.wizard/bin:$PATH\"\nexport WIZARD_ROOT=\"/home/u/.wizard/current\"\n"
	got, changed := stripWizardShellIntegration(in, testInstall)
	if !changed || strings.Contains(got, "Wizard CLI") || strings.Contains(got, ".wizard") {
		t.Fatalf("legacy lines not removed:\n%s", got)
	}
	if !strings.Contains(got, "export PATH=\"$HOME/bin:$PATH\"") {
		t.Fatalf("the user's own PATH line must survive:\n%s", got)
	}
}

func TestStripLeavesUnrelatedFilesUntouched(t *testing.T) {
	in := "# Wizard CLI is great\nexport PATH=\"/opt/other/bin:$PATH\"\nexport WIZARD_ROOT=\"/somewhere/else\"\n"
	got, changed := stripWizardShellIntegration(in, testInstall)
	if changed || got != in {
		t.Fatalf("nothing of ours was present; changed=%v\n%s", changed, got)
	}
}

func TestStripFishLegacyLines(t *testing.T) {
	in := "# Wizard CLI\nfish_add_path /home/u/.wizard/bin\nset -gx WIZARD_ROOT \"/home/u/.wizard/current\"\nset -gx X 1\n"
	got, _ := stripWizardShellIntegration(in, testInstall)
	if strings.Contains(got, "wizard") || !strings.Contains(got, "set -gx X 1") {
		t.Fatalf("got:\n%s", got)
	}
}

func TestStripIsIdempotent(t *testing.T) {
	in := rcBlockStart + "\nx\n" + rcBlockEnd + "\n"
	once, _ := stripWizardShellIntegration(in, testInstall)
	twice, changed := stripWizardShellIntegration(once, testInstall)
	if changed || once != twice {
		t.Fatalf("second pass changed things: %q -> %q", once, twice)
	}
}

func TestRemoveShellIntegrationKeepsSymlinkedDotfiles(t *testing.T) {
	home := t.TempDir()
	repoDir := t.TempDir()
	real := filepath.Join(repoDir, "zshrc")
	if err := os.WriteFile(real, []byte("export A=1\n"+rcBlockStart+"\n. x\n"+rcBlockEnd+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(home, ".zshrc")
	if err := os.Symlink(real, link); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	changed, err := removeShellIntegration(home, testInstall)
	if err != nil || len(changed) != 1 {
		t.Fatalf("changed = %v, err = %v", changed, err)
	}
	if info, _ := os.Lstat(link); info.Mode()&os.ModeSymlink == 0 {
		t.Fatal("the dotfile symlink was replaced by a regular file")
	}
	if data, _ := os.ReadFile(real); strings.Contains(string(data), "wizard") || !strings.Contains(string(data), "export A=1") {
		t.Fatalf("target content wrong: %q", data)
	}
}

func TestRemoveShellIntegrationRemovesFishDropIn(t *testing.T) {
	home := t.TempDir()
	dropIn := filepath.Join(home, ".config", "fish", "conf.d", "wizard.fish")
	if err := os.MkdirAll(filepath.Dir(dropIn), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dropIn, []byte(rcBlockStart+"\nfish_add_path x\n"+rcBlockEnd+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// A conf.d file that merely has the same name but is not ours stays.
	other := filepath.Join(filepath.Dir(dropIn), "keep.fish")
	_ = os.WriteFile(other, []byte("set x 1\n"), 0o644)
	if _, err := removeShellIntegration(home, testInstall); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dropIn); err == nil {
		t.Fatal("fish drop-in should be removed")
	}
	if _, err := os.Stat(other); err != nil {
		t.Fatal("unrelated fish config must remain")
	}
}
