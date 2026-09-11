//go:build windows

package commands

import (
	"os"
	"path/filepath"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// platformToolPaths returns conventional install locations plus the current
// user/system PATH values from the registry. Winget updates those persistent
// values, but a process that was already running does not receive the
// environment refresh that a new shell would receive.
func platformToolPaths(home string) []string {
	paths := make([]string, 0, 24)
	add := func(base string, parts ...string) {
		if base == "" {
			return
		}
		paths = append(paths, filepath.Join(append([]string{base}, parts...)...))
	}

	localAppData := os.Getenv("LOCALAPPDATA")
	programFiles := os.Getenv("ProgramFiles")
	appData := os.Getenv("APPDATA")

	add(programFiles, "nodejs")
	add(appData, "npm")
	add(appData, "pnpm")
	add(localAppData, "pnpm")
	add(localAppData, "Microsoft", "WinGet", "Links")
	add(home, ".local", "bin")
	add(home, ".cargo", "bin")

	for _, match := range windowsPythonInstallDirs() {
		paths = append(paths, match, filepath.Join(match, "Scripts"))
	}

	paths = append(paths, windowsRegistryPathEntries()...)
	return paths
}

func platformPythonCandidates() []string {
	paths := make([]string, 0, 8)
	for _, directory := range windowsPythonInstallDirs() {
		paths = append(paths, filepath.Join(directory, "python.exe"))
	}
	return paths
}

func windowsPythonInstallDirs() []string {
	localAppData := os.Getenv("LOCALAPPDATA")
	programFiles := os.Getenv("ProgramFiles")
	programFilesX86 := os.Getenv("ProgramFiles(x86)")
	patterns := make([]string, 0, 5)
	if localAppData != "" {
		patterns = append(patterns, filepath.Join(localAppData, "Programs", "Python", "Python3*"))
	}
	for _, base := range []string{programFiles, programFilesX86} {
		if base != "" {
			patterns = append(patterns,
				filepath.Join(base, "Python3*"),
				filepath.Join(base, "Python", "Python3*"),
			)
		}
	}
	directories := make([]string, 0, 8)
	for _, pattern := range patterns {
		matches, _ := filepath.Glob(pattern)
		for _, match := range matches {
			info, err := os.Stat(match)
			if err == nil && info.IsDir() {
				directories = append(directories, match)
			}
		}
	}
	return directories
}

func windowsRegistryPathEntries() []string {
	locations := []struct {
		root registry.Key
		path string
	}{
		{registry.CURRENT_USER, `Environment`},
		{registry.LOCAL_MACHINE, `SYSTEM\CurrentControlSet\Control\Session Manager\Environment`},
	}
	entries := make([]string, 0, 16)
	for _, location := range locations {
		key, err := registry.OpenKey(location.root, location.path, registry.QUERY_VALUE)
		if err != nil {
			continue
		}
		raw, _, err := key.GetStringValue("Path")
		_ = key.Close()
		if err != nil || raw == "" {
			continue
		}
		entries = append(entries, filepath.SplitList(expandWindowsEnvironment(raw))...)
	}
	return entries
}

func expandWindowsEnvironment(value string) string {
	source, err := windows.UTF16PtrFromString(value)
	if err != nil {
		return value
	}
	// Windows environment blocks are limited to 32,767 characters. Keep one
	// extra slot for the terminating NUL expected by ExpandEnvironmentStrings.
	buffer := make([]uint16, 32768)
	n, err := windows.ExpandEnvironmentStrings(source, &buffer[0], uint32(len(buffer)))
	if err != nil || n == 0 || n > uint32(len(buffer)) {
		return value
	}
	return windows.UTF16ToString(buffer[:n])
}
