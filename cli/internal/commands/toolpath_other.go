//go:build !windows

package commands

import "path/filepath"

func platformToolPaths(home string) []string {
	return []string{
		filepath.Join(home, ".local", "bin"),
		filepath.Join(home, ".cargo", "bin"),
		"/usr/local/bin",
		"/opt/homebrew/bin",
	}
}

func platformPythonCandidates() []string { return nil }
