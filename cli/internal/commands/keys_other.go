//go:build !windows

package commands

import (
	"os"
	"time"

	"golang.org/x/sys/unix"
)

// inputPending reports whether the terminal has a byte to read within timeout.
// It is how a lone Escape key is told apart from the start of an arrow-key
// sequence: the rest of a sequence arrives with the first byte, a keypress does
// not. On any error it answers true, which falls back to the plain blocking read.
func inputPending(file *os.File, timeout time.Duration) bool {
	fds := []unix.PollFd{{Fd: int32(file.Fd()), Events: unix.POLLIN}}
	n, err := unix.Poll(fds, int(timeout/time.Millisecond))
	return err != nil || n > 0
}
