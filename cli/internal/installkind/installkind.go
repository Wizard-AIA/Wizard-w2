// Package installkind answers "who owns this Wizard installation?" so that
// update, uninstall and doctor never overwrite files a package manager owns.
//
// Detection is by layout, not by asking the package manager: shelling out to
// brew or scoop is slow, may be absent from PATH, and would not work for a
// binary that was copied elsewhere.
package installkind

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// Kind is how the running installation was put on disk.
type Kind string

const (
	// Unknown is an extracted archive or any layout none of the others match.
	Unknown Kind = "unmanaged"
	// Checkout is a git working tree (a source install).
	Checkout Kind = "git checkout"
	// Direct is the layout the official install scripts create:
	// <install>/bin/wizard plus <install>/current -> <install>/Wizard-<tag>-<os>-<arch>.
	Direct Kind = "release installer"
	// Homebrew is a formula keg under a Cellar.
	Homebrew Kind = "Homebrew"
	// Scoop is a package under scoop/apps/wizard.
	Scoop Kind = "Scoop"
)

// Info is the result of Detect.
type Info struct {
	Kind Kind
	// Exe and Root are the symlink-resolved executable and checkout paths.
	Exe  string
	Root string
	// InstallRoot is set for Direct installs only.
	InstallRoot string
}

// PackageManaged reports whether a package manager owns the files, in which
// case Wizard must not modify or delete them itself.
func (k Kind) PackageManaged() bool { return k == Homebrew || k == Scoop }

// UpgradeCommand is what the user should run to upgrade, or "" when Wizard can
// upgrade itself or the caller should use git.
func (k Kind) UpgradeCommand() string {
	switch k {
	case Homebrew:
		return "brew update && brew upgrade wizard"
	case Scoop:
		return "scoop update wizard"
	}
	return ""
}

// UninstallCommand is what the user should run to remove the installation, or
// "" when `wizard uninstall` handles it.
func (k Kind) UninstallCommand() string {
	switch k {
	case Homebrew:
		return "brew uninstall wizard"
	case Scoop:
		return "scoop uninstall wizard"
	}
	return ""
}

// Detect classifies the installation. exe is the running binary, root the
// checkout directory repo.Root resolved; either may be empty.
func Detect(exe, root string) Info {
	info := Info{Kind: Unknown, Exe: resolve(exe), Root: resolve(root)}
	switch {
	case hasSegments(info.Exe, "Cellar", "wizard") || hasSegments(info.Root, "Cellar", "wizard"):
		info.Kind = Homebrew
	case hasSegmentsFold(info.Exe, "scoop", "apps", "wizard") || hasSegmentsFold(info.Root, "scoop", "apps", "wizard"):
		info.Kind = Scoop
	default:
		if installRoot, err := ManagedInstallRoot(info.Root); err == nil {
			info.Kind = Direct
			info.InstallRoot = installRoot
		} else if info.Root != "" && exists(filepath.Join(info.Root, ".git")) {
			info.Kind = Checkout
		}
	}
	return info
}

// ManagedInstallRoot recognizes only the layout the official installers
// create: <install>/current is the active package and <install>/bin holds the
// launcher. Refusing arbitrary extracted folders keeps an update or uninstall
// from touching a developer checkout or an unrelated parent directory.
func ManagedInstallRoot(repoRoot string) (string, error) {
	if repoRoot == "" {
		return "", fmt.Errorf("no active package")
	}
	resolvedRoot, err := filepath.EvalSymlinks(repoRoot)
	if err != nil {
		return "", fmt.Errorf("resolving active package: %w", err)
	}
	installRoot := filepath.Dir(resolvedRoot)
	resolvedCurrent, err := filepath.EvalSymlinks(filepath.Join(installRoot, "current"))
	if err != nil {
		return "", fmt.Errorf("missing managed current release pointer")
	}
	if filepath.Clean(resolvedCurrent) != filepath.Clean(resolvedRoot) {
		return "", fmt.Errorf("active package is not the managed current release")
	}
	launcher := filepath.Join(installRoot, "bin", launcherName())
	if info, err := os.Stat(launcher); err != nil || info.IsDir() {
		return "", fmt.Errorf("missing managed wizard launcher")
	}
	return installRoot, nil
}

func launcherName() string {
	if runtime.GOOS == "windows" {
		return "wizard.exe"
	}
	return "wizard"
}

func resolve(p string) string {
	if p == "" {
		return ""
	}
	if r, err := filepath.EvalSymlinks(p); err == nil {
		return r
	}
	return filepath.Clean(p)
}

func exists(p string) bool { _, err := os.Lstat(p); return err == nil }

func segments(p string) []string {
	return strings.FieldsFunc(p, func(r rune) bool { return r == '/' || r == '\\' })
}

// hasSegments reports whether want appears as consecutive, case-sensitive path
// components.
func hasSegments(p string, want ...string) bool { return containsRun(segments(p), want, false) }

// hasSegmentsFold is hasSegments ignoring case, for Windows paths.
func hasSegmentsFold(p string, want ...string) bool { return containsRun(segments(p), want, true) }

func containsRun(have, want []string, fold bool) bool {
	for i := 0; i+len(want) <= len(have); i++ {
		match := true
		for j, w := range want {
			h := have[i+j]
			if fold {
				h, w = strings.ToLower(h), strings.ToLower(w)
			}
			if h != w {
				match = false
				break
			}
		}
		if match {
			return true
		}
	}
	return false
}
