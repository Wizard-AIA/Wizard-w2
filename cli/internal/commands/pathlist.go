package commands

import (
	"path/filepath"
	"strings"
)

// pathListWithout returns a semicolon-separated Windows PATH value with every
// entry equal to dir removed, and whether anything changed. Entries are
// compared case-insensitively and ignoring quotes and a trailing backslash,
// but otherwise left byte-for-byte alone -- in particular %VARIABLE% references
// stay unexpanded, because the caller writes the value back as REG_EXPAND_SZ.
// Empty segments are preserved so nobody else's PATH is reformatted.
func pathListWithout(raw, dir string) (string, bool) {
	target := normalizePathEntry(dir)
	parts := strings.Split(raw, ";")
	kept := make([]string, 0, len(parts))
	changed := false
	for _, part := range parts {
		if part != "" && normalizePathEntry(part) == target {
			changed = true
			continue
		}
		kept = append(kept, part)
	}
	return strings.Join(kept, ";"), changed
}

func normalizePathEntry(entry string) string {
	entry = strings.Trim(strings.TrimSpace(entry), `"`)
	entry = strings.TrimRight(entry, `\/`)
	return strings.ToLower(filepath.ToSlash(entry))
}
