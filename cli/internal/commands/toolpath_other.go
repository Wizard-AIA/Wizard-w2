//go:build !windows

package commands

import (
	"fmt"
	"path/filepath"
)

func platformToolPaths(home string) []string {
	// Versioned Homebrew formulae are keg-only: they are never linked into
	// bin/, so the fallback `node@<min>` install is only reachable through its
	// own opt/ directory.
	nodeKeg := fmt.Sprintf("opt/node@%d/bin", minNodeMajor)
	return []string{
		filepath.Join(home, ".local", "bin"),
		filepath.Join(home, ".cargo", "bin"),
		filepath.Join(home, ".local", "share", "pnpm"), // pnpm's standalone installer (Linux)
		filepath.Join(home, "Library", "pnpm"),         // ... and macOS
		"/usr/local/bin",
		"/opt/homebrew/bin",
		"/opt/homebrew/" + nodeKeg,
		"/usr/local/" + nodeKeg,
		"/home/linuxbrew/.linuxbrew/" + nodeKeg,
	}
}

func platformPythonCandidates() []string { return nil }

// persistedPathEntries are PATH entries stored outside this process (the
// Windows registry). Unix shells inherit PATH, so there is nothing to read.
func persistedPathEntries() []string { return nil }
