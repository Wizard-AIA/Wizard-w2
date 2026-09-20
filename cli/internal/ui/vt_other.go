//go:build !windows

package ui

import "io"

// enableVT reports whether ANSI escapes work. Unix terminals always handle them.
func enableVT(io.Writer) bool { return true }
