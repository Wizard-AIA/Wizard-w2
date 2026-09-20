package commands

import (
	"os"
	"path/filepath"
	"strings"
)

// The official installer exposes `wizard` to shells by writing one marked
// block that sources <install>/env. Uninstall removes exactly that block, plus
// the loose lines the 1.0.x installers appended, and nothing else.
const (
	rcBlockStart = "# >>> wizard (managed by the Wizard installer) >>>"
	rcBlockEnd   = "# <<< wizard <<<"
	rcLegacyTag  = "# Wizard CLI"
)

// stripWizardShellIntegration returns content without Wizard's PATH/WIZARD_ROOT
// setup. installRoot scopes the legacy-line removal: only lines that mention
// this install are dropped, so a user's own `export PATH=` lines survive.
func stripWizardShellIntegration(content, installRoot string) (string, bool) {
	lines := strings.Split(content, "\n")
	out := make([]string, 0, len(lines))
	changed := false
	inBlock := false

	isLegacyWizardLine := func(line string) bool {
		trimmed := strings.TrimSpace(line)
		if installRoot == "" || !strings.Contains(trimmed, installRoot) {
			return false
		}
		for _, prefix := range []string{"export PATH=", "export WIZARD_ROOT=", "fish_add_path ", "set -gx WIZARD_ROOT "} {
			if strings.HasPrefix(trimmed, prefix) {
				return true
			}
		}
		return false
	}

	for i := 0; i < len(lines); i++ {
		line := lines[i]
		switch {
		case strings.TrimSpace(line) == rcBlockStart:
			inBlock, changed = true, true
			continue
		case inBlock:
			if strings.TrimSpace(line) == rcBlockEnd {
				inBlock = false
			}
			continue
		case strings.TrimSpace(line) == rcLegacyTag:
			// The legacy comment introduces the lines below it; drop the
			// comment only if at least one of them is ours.
			j := i + 1
			dropped := false
			for j < len(lines) && isLegacyWizardLine(lines[j]) {
				j++
				dropped = true
			}
			if dropped {
				i = j - 1
				changed = true
				continue
			}
		case isLegacyWizardLine(line):
			changed = true
			continue
		}
		out = append(out, line)
	}
	result := strings.Join(out, "\n")
	// Removing a block leaves the blank line that preceded it; collapse a
	// run of trailing blank lines to the single newline the file had.
	if changed {
		result = strings.TrimRight(result, "\n") + "\n"
	}
	return result, changed
}

// shellStartupFiles are the files the installer or older installers may have
// edited, relative to home.
func shellStartupFiles(home string) []string {
	return []string{
		filepath.Join(home, ".zshrc"),
		filepath.Join(home, ".zshenv"),
		filepath.Join(home, ".bashrc"),
		filepath.Join(home, ".bash_profile"),
		filepath.Join(home, ".profile"),
		filepath.Join(home, ".config", "fish", "config.fish"),
	}
}

// removeShellIntegration cleans every startup file and returns the ones it
// changed. Files are rewritten in place, so a dotfile that is a symlink into a
// dotfiles repository stays a symlink.
func removeShellIntegration(home, installRoot string) ([]string, error) {
	var changedFiles []string
	for _, path := range shellStartupFiles(home) {
		data, err := os.ReadFile(path)
		if err != nil {
			continue // absent or unreadable: nothing of ours to remove
		}
		stripped, changed := stripWizardShellIntegration(string(data), installRoot)
		if !changed {
			continue
		}
		info, err := os.Stat(path)
		if err != nil {
			return changedFiles, err
		}
		if err := os.WriteFile(path, []byte(stripped), info.Mode().Perm()); err != nil {
			return changedFiles, err
		}
		changedFiles = append(changedFiles, path)
	}
	// Fish loads drop-ins from conf.d; the installer owns this file outright.
	dropIn := filepath.Join(home, ".config", "fish", "conf.d", "wizard.fish")
	if data, err := os.ReadFile(dropIn); err == nil && strings.Contains(string(data), rcBlockStart) {
		if err := os.Remove(dropIn); err != nil {
			return changedFiles, err
		}
		changedFiles = append(changedFiles, dropIn)
	}
	return changedFiles, nil
}
