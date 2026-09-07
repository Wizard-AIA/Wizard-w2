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
				// Release installers retain old packages for rollback and move
				// the stable current pointer during an update. A persisted
				// WIZARD_ROOT may still name the old package, so prefer its
				// sibling current pointer when it is a valid checkout.
				if current := filepath.Join(filepath.Dir(envVal), "current"); looksLikeCheckout(current) {
					return current, nil
				}
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

	// 3. Check the executable's checkout and the installed-package layout
	// (e.g. Homebrew Cellar or the standalone installer). Windows keeps a copy
	// of the binary in <install>/bin while <install>/current points at the
	// checkout, so the checkout is not an ancestor of the executable there.
	if exe, err := os.Executable(); err == nil {
		if root, err := rootFromExecutable(exe); err == nil {
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

func rootFromExecutable(exe string) (string, error) {
	dirs := []string{filepath.Dir(exe)}
	if realExe, err := filepath.EvalSymlinks(exe); err == nil {
		dirs = append([]string{filepath.Dir(realExe)}, dirs...)
	}

	for _, dir := range dirs {
		if root, err := RootFrom(dir); err == nil {
			return root, nil
		}
		// The Linux/macOS installer and Windows installer both maintain this
		// stable pointer beside bin/: <install>/current -> <checkout>.
		current := filepath.Join(filepath.Dir(dir), "current")
		if root, err := RootFrom(current); err == nil {
			return root, nil
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
