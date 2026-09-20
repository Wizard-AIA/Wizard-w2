//go:build windows

package ui

import (
	"io"
	"os"

	"golang.org/x/sys/windows"
)

// enableVT turns on ANSI escape processing for w's console. Windows Terminal
// and PowerShell 7 have it on already; Windows PowerShell 5.1 in conhost needs
// it requested. If the console refuses, the caller falls back to plain text.
func enableVT(w io.Writer) bool {
	f, ok := w.(*os.File)
	if !ok {
		return false
	}
	handle := windows.Handle(f.Fd())
	var mode uint32
	if err := windows.GetConsoleMode(handle, &mode); err != nil {
		return false
	}
	if mode&windows.ENABLE_VIRTUAL_TERMINAL_PROCESSING != 0 {
		return true
	}
	return windows.SetConsoleMode(handle, mode|windows.ENABLE_VIRTUAL_TERMINAL_PROCESSING) == nil
}

// EnableVTInput asks the Windows console to deliver arrow and function keys as
// ANSI sequences (ESC [ A ...) instead of key events, which is what the menu's
// key decoder reads. Windows Terminal and PowerShell 7 usually have it on; the
// legacy console does not until requested. The returned func restores the
// previous input mode.
func EnableVTInput(f *os.File) (restore func()) {
	handle := windows.Handle(f.Fd())
	var mode uint32
	if err := windows.GetConsoleMode(handle, &mode); err != nil {
		return func() {}
	}
	if mode&windows.ENABLE_VIRTUAL_TERMINAL_INPUT != 0 {
		return func() {}
	}
	if err := windows.SetConsoleMode(handle, mode|windows.ENABLE_VIRTUAL_TERMINAL_INPUT); err != nil {
		return func() {}
	}
	return func() { _ = windows.SetConsoleMode(handle, mode) }
}
