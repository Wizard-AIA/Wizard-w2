//go:build !windows

package ui

import (
	"io"
	"os"
)

// enableVT reports whether ANSI escapes work. Unix terminals always handle them.
func enableVT(io.Writer) bool { return true }

// EnableVTInput makes a terminal deliver arrow and function keys as ANSI
// sequences. Unix terminals already do; the returned func undoes the change.
func EnableVTInput(*os.File) (restore func()) { return func() {} }
