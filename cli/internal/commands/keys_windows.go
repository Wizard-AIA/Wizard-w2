//go:build windows

package commands

import (
	"os"
	"time"

	"golang.org/x/sys/windows"
)

// inputPending reports whether the console has input to read within timeout;
// see keys_other.go. A console input handle is signalled while events are queued.
func inputPending(file *os.File, timeout time.Duration) bool {
	event, err := windows.WaitForSingleObject(windows.Handle(file.Fd()), uint32(timeout/time.Millisecond))
	return err != nil || event != uint32(windows.WAIT_TIMEOUT)
}
