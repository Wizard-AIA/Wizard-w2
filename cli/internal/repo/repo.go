// Package repo locates the Wizard checkout the CLI is meant to manage.
//
// `wizard` is expected to be run from inside the clone (or a subdirectory of
// it), the same way `git`/`npm` locate their project root -- or via WIZARD_ROOT
// environment variable or package install directories.
package repo

import (
	"errors"
	"os"
	"path/filepath"
)

// ErrNotFound means no ancestor of the starting directory or installation location looks like a
// Wizard checkout (has both backend/main.py and frontend/package.json).
var ErrNotFound = errors.New("not inside a Wizard checkout (no backend/main.py + frontend/package.json found in this directory, its parents, or WIZARD_ROOT).\n\n" +
	"To run Wizard:\n" +
	"  1. Navigate to your Wizard directory:  cd /path/to/Wizard-w2\n" +
	"  2. Or set the WIZARD_ROOT environment variable:  export WIZARD_ROOT=/path/to/Wizard-w2\n" +
	"  3. Or clone a fresh workspace:  git clone https://github.com/Wizard-AIA/Wizard-w2.git && cd Wizard-w2")

// Root walks up from the current working directory, checks environment variables,
// executable binary parents, and standard paths looking for a directory
// containing both backend/main.py and frontend/package.json.
func Root() (string, error) {
	// 1. Check explicit environment variables
	for _, envVar := range []string{"WIZARD_ROOT", "WIZARD_HOME", "WIZARD_DIR"} {
		if envVal := os.Getenv(envVar); envVal != "" {
			if looksLikeCheckout(envVal) {
				return envVal, nil
			}
			if root, err := RootFrom(envVal); err == nil {
				return root, nil
			}
		}
	}

	// 2. Check current working directory and its ancestors
	if dir, err := os.Getwd(); err == nil {
		if root, err := RootFrom(dir); err == nil {
			return root, nil
		}
	}

	// 3. Check executable binary directory and its ancestors (e.g. Homebrew Cellar / opt)
	if exe, err := os.Executable(); err == nil {
		if realExe, err := filepath.EvalSymlinks(exe); err == nil {
			if root, err := RootFrom(filepath.Dir(realExe)); err == nil {
				return root, nil
			}
		}
		if root, err := RootFrom(filepath.Dir(exe)); err == nil {
			return root, nil
		}
	}

	// 4. Check common default paths
	if home, err := os.UserHomeDir(); err == nil && home != "" {
		candidates := []string{
			filepath.Join(home, ".wizard"),
			filepath.Join(home, "Wizard-w2"),
			filepath.Join(home, "Projects", "Wizard-w2"),
			"/opt/homebrew/opt/wizard",
			"/usr/local/opt/wizard",
			"/opt/homebrew/share/wizard",
			"/usr/local/share/wizard",
		}
		for _, candidate := range candidates {
			if candidate != "" && looksLikeCheckout(candidate) {
				return candidate, nil
			}
		}
	}

	return "", ErrNotFound
}

// RootFrom is Root's testable core: the starting directory is a parameter
// instead of os.Getwd().
func RootFrom(start string) (string, error) {
	dir := start
	for {
		if looksLikeCheckout(dir) {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", ErrNotFound
		}
		dir = parent
	}
}

func looksLikeCheckout(dir string) bool {
	_, err1 := os.Stat(filepath.Join(dir, "backend", "main.py"))
	_, err2 := os.Stat(filepath.Join(dir, "frontend", "package.json"))
	return err1 == nil && err2 == nil
}

// BackendDir and FrontendDir are convenience joins off Root.
func BackendDir(root string) string  { return filepath.Join(root, "backend") }
func FrontendDir(root string) string { return filepath.Join(root, "frontend") }

