// Package repo locates the Wizard checkout the CLI is meant to manage.
//
// `wizard` is expected to be run from inside the clone (or a subdirectory of
// it), the same way `git`/`npm` locate their project root -- rather than
// requiring a config file pointing at the checkout, which is one more thing
// to get out of sync.
package repo

import (
	"errors"
	"os"
	"path/filepath"
)

// ErrNotFound means no ancestor of the starting directory looks like a
// Wizard checkout (has both backend/main.py and frontend/package.json).
var ErrNotFound = errors.New("not inside a Wizard checkout (no backend/main.py + frontend/package.json found in this directory or any parent)")

// Root walks up from the current working directory looking for a directory
// containing both backend/main.py and frontend/package.json -- present
// together only at the checkout root, never in a subdirectory of either.
func Root() (string, error) {
	if dir, err := os.Getwd(); err == nil {
		if root, err := RootFrom(dir); err == nil {
			return root, nil
		}
	}
	if exe, err := os.Executable(); err == nil {
		if realExe, err := filepath.EvalSymlinks(exe); err == nil {
			if root, err := RootFrom(filepath.Dir(realExe)); err == nil {
				return root, nil
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
