package commands

import (
	"fmt"
	"os"
	"path/filepath"
)

// managedInstallRoot recognizes only the layout created by the official
// installers: <install>/current is the active package and <install>/bin holds
// the launcher. Refusing arbitrary extracted folders prevents an update from
// renaming a developer checkout or an unrelated parent directory.
func managedInstallRoot(repoRoot string) (string, error) {
	resolvedRoot, err := filepath.EvalSymlinks(repoRoot)
	if err != nil {
		return "", fmt.Errorf("resolving active package: %w", err)
	}
	installRoot := filepath.Dir(resolvedRoot)
	current := filepath.Join(installRoot, "current")
	resolvedCurrent, err := filepath.EvalSymlinks(current)
	if err != nil {
		return "", fmt.Errorf("missing managed current release pointer")
	}
	if filepath.Clean(resolvedCurrent) != filepath.Clean(resolvedRoot) {
		return "", fmt.Errorf("active package is not the managed current release")
	}
	if info, err := os.Stat(filepath.Join(installRoot, "bin", "wizard"+executableSuffix())); err != nil || info.IsDir() {
		return "", fmt.Errorf("missing managed wizard launcher")
	}
	return installRoot, nil
}
