package commands

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// versionManagerBins lists the bin directories of Node version managers and
// shim directories under home, newest Node first. A shell that loads nvm (or
// fnm, volta, asdf, mise) lazily -- or a program launched from a GUI -- does not
// have these on PATH even though Node and pnpm are installed, and reporting
// them "missing" would send the user to install a second copy.
//
// They are only ever appended after the user's own PATH, so a tool that is
// already on PATH always wins; these are a fallback for when it is not.
func versionManagerBins(home string) []string {
	if home == "" {
		return nil
	}
	var dirs []string
	// Managers that keep one directory per installed Node version.
	for _, pattern := range []string{
		filepath.Join(home, ".nvm", "versions", "node", "*", "bin"),
		filepath.Join(home, ".local", "share", "fnm", "node-versions", "*", "installation", "bin"),
		filepath.Join(home, "Library", "Application Support", "fnm", "node-versions", "*", "installation", "bin"),
		filepath.Join(home, ".fnm", "node-versions", "*", "installation", "bin"),
	} {
		matches, _ := filepath.Glob(pattern)
		sortVersionDirsDesc(matches)
		dirs = append(dirs, matches...)
	}
	// Managers that expose a single shim directory.
	for _, shim := range []string{
		filepath.Join(home, ".volta", "bin"),
		filepath.Join(home, ".asdf", "shims"),
		filepath.Join(home, ".local", "share", "mise", "shims"),
	} {
		dirs = append(dirs, shim)
	}
	existing := dirs[:0]
	for _, d := range dirs {
		if info, err := os.Stat(d); err == nil && info.IsDir() {
			existing = append(existing, d)
		}
	}
	return existing
}

// sortVersionDirsDesc orders paths like .../v22.11.0/bin newest version first,
// comparing the numeric components rather than the text (v9 sorts before v22).
func sortVersionDirsDesc(paths []string) {
	key := func(p string) [3]int {
		// The version is the path component that starts with "v" or a digit.
		for _, part := range strings.Split(filepath.ToSlash(p), "/") {
			part = strings.TrimPrefix(part, "v")
			fields := strings.Split(part, ".")
			if len(fields) < 2 {
				continue
			}
			var v [3]int
			ok := true
			for i := 0; i < 3 && i < len(fields); i++ {
				n, err := strconv.Atoi(fields[i])
				if err != nil {
					ok = false
					break
				}
				v[i] = n
			}
			if ok {
				return v
			}
		}
		return [3]int{}
	}
	sort.SliceStable(paths, func(i, j int) bool {
		a, b := key(paths[i]), key(paths[j])
		for k := range a {
			if a[k] != b[k] {
				return a[k] > b[k]
			}
		}
		return paths[i] < paths[j]
	})
}
