package daemon

import (
	"io"
	"os"
	"os/exec"
	"time"
)

// Child is one supervised subprocess (the backend or the frontend server),
// spawned into its own process group (POSIX) or process group + console
// (Windows) so that stopping it does not also signal this process, and so a
// forced kill can reach anything it spawned. The platform-specific half of
// Start/Terminate/IsAlive lives in process_unix.go and process_windows.go --
// build-tag filenames, Go's standard way of giving each platform its own
// file, matching the split backend/src/core/security/sandbox already uses
// for the same reason.
type Child struct {
	Name string // "backend" or "frontend", for log/error messages only
	cmd  *exec.Cmd
	done chan struct{}
	err  error
}

// NewChild builds (but does not start) a Child.
func NewChild(label, name string, args []string, dir string, env []string, out io.Writer) *Child {
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Env = env
	cmd.Stdout = out
	cmd.Stderr = out
	applyPlatformAttrs(cmd)
	return &Child{Name: label, cmd: cmd}
}

// Start launches the process and begins waiting on it in the background;
// Done() closes once it exits, for any reason.
func (c *Child) Start() error {
	if err := c.cmd.Start(); err != nil {
		return err
	}
	c.done = make(chan struct{})
	go func() {
		c.err = c.cmd.Wait()
		close(c.done)
	}()
	return nil
}

// Pid is valid once Start has returned successfully.
func (c *Child) Pid() int { return c.cmd.Process.Pid }

// Done closes when the process has exited.
func (c *Child) Done() <-chan struct{} { return c.done }

// ExitErr is only meaningful after Done() has closed.
func (c *Child) ExitErr() error { return c.err }

// Terminate asks the process to stop, escalating to a forced kill if it has
// not exited within grace. Safe to call once Start has succeeded; returns
// once the process is confirmed gone.
func (c *Child) Terminate(grace time.Duration) error {
	return terminateChild(c, grace)
}

// StartDetached launches the `__supervise` process itself: fully detached
// from `wizard start`'s console (own process group; DETACHED_PROCESS on
// Windows) so it outlives the command that launched it and its terminal
// closing, with its own stdout/stderr appended to logPath rather than
// inherited. Returns the new process's pid; the caller does not wait on it.
func StartDetached(name string, args []string, dir string, env []string, logPath string) (int, error) {
	log, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return 0, err
	}
	// The child inherits its own reference to this handle for its stdout/stderr,
	// so a close error on the parent's copy at return has nothing left to do
	// about it.
	defer func() { _ = log.Close() }()

	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Env = env
	cmd.Stdout = log
	cmd.Stderr = log
	detachAttrs(cmd)

	if err := cmd.Start(); err != nil {
		return 0, err
	}
	pid := cmd.Process.Pid
	// Release rather than Wait: this process's job is to hand off and exit,
	// not to babysit the supervisor it just started.
	_ = cmd.Process.Release()
	return pid, nil
}
