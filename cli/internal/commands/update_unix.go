//go:build !windows

package commands

import (
	"fmt"
	"os"
	"path/filepath"
)

// activateStagedRelease uses atomic renames of the stable launcher and current
// symlink. The old package is intentionally retained as a rollback target.
func activateStagedRelease(installRoot, stageDir, packageDir, tag string, restart bool, backend, frontend string) (bool, error) {
	packageName := filepath.Base(packageDir)
	destination := filepath.Join(installRoot, packageName)
	if _, err := os.Lstat(destination); err == nil {
		return false, fmt.Errorf("release package %s already exists", packageName)
	} else if !os.IsNotExist(err) {
		return false, err
	}
	if err := os.Rename(packageDir, destination); err != nil {
		return false, fmt.Errorf("moving verified release into place: %w", err)
	}
	binDir := filepath.Join(installRoot, "bin")
	launcher := filepath.Join(binDir, "wizard")
	launcherNext := launcher + ".next"
	current := filepath.Join(installRoot, "current")
	currentNext := current + ".next"
	_ = os.Remove(launcherNext)
	_ = os.Remove(currentNext)
	if err := os.Symlink(filepath.Join("..", "current", "cli", "wizard"), launcherNext); err != nil {
		return false, fmt.Errorf("preparing updated launcher: %w", err)
	}
	// Updating the launcher first is safe: it still resolves the old current
	// package until the following rename succeeds.
	if err := os.Rename(launcherNext, launcher); err != nil {
		return false, fmt.Errorf("activating updated launcher: %w", err)
	}
	if err := os.Symlink(packageName, currentNext); err != nil {
		return false, fmt.Errorf("preparing current release pointer: %w", err)
	}
	if err := os.Rename(currentNext, current); err != nil {
		return false, fmt.Errorf("activating current release pointer: %w", err)
	}
	return false, nil
}

// The hidden Windows helper is never callable on Unix-like systems.
func RunApplyStagedUpdate(args []string) int { return 2 }
