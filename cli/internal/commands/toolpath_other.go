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

// persistedPathEntries are PATH entries stored outside this process (the
// Windows registry). Unix shells inherit PATH, so there is nothing to read.
func persistedPathEntries() []string { return nil }
