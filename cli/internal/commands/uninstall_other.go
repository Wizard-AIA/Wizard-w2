//go:build !windows

package commands

// Unix shells inherit PATH from startup files, which removeShellIntegration
// already cleaned; there is no persisted PATH to edit.
func removePersistedPath(binDir, installRoot string) error { return nil }

// A running binary can be unlinked on Unix, so nothing is ever deferred.
func scheduleSelfCleanup(installRoot string) {}

func inUse(err error) bool { return false }
