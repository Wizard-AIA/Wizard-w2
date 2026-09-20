package commands

import (
	"fmt"
	"os"
	"time"

	"wizard/internal/daemon"
)

// RunStop implements `wizard stop`. Idempotent: stopping an already-stopped
// daemon is success, not an error, the same way `docker compose down` on an
// already-down stack is.
func RunStop(env *Env, args []string) int {
	if code, done := noFlags(env, "stop", args); done {
		return code
	}
	_, alive := daemon.LiveAt(env.DaemonPIDPath())
	if !alive {
		// A previous run may have left the stop sentinel or stale pid files
		// behind (e.g. `wizard start` gave up waiting for a clean exit --
		// see requestStop in start.go). Left in place, the sentinel makes
		// the *next* `wizard start` shut its own supervisor down at the
		// first poll tick.
		_ = os.Remove(env.StopSentinelPath())
		_ = daemon.RemovePID(env.BackendPIDPath())
		_ = daemon.RemovePID(env.FrontendPIDPath())
		_ = daemon.RemovePID(env.DaemonPIDPath())
		fmt.Fprintln(env.Out, "wizard is not running.")
		return 0
	}

	fmt.Fprintln(env.Out, "Stopping...")
	if err := os.WriteFile(env.StopSentinelPath(), []byte("1"), 0o644); err != nil {
		fmt.Fprintf(env.Err, "Could not request a stop: %v\n", err)
		return 1
	}

	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if _, alive := daemon.LiveAt(env.DaemonPIDPath()); !alive {
			fmt.Fprintln(env.Out, "Stopped.")
			return 0
		}
		time.Sleep(300 * time.Millisecond)
	}

	fmt.Fprintln(env.Out, "The supervisor did not exit on its own in time; forcing a stop.")
	forceKill(env)
	fmt.Fprintln(env.Out, "Stopped.")
	return 0
}

// forceKill re-reads each pid file itself, right before killing, rather than
// trusting a pid captured up to 20 seconds earlier (the wait loop above) --
// LiveAt's ownership check (see pidfile.go) only protects against a pid the
// OS has recycled onto an unrelated process if it is asked again close to
// the kill, not once at the start of a wait that pid could have outlived.
func forceKill(env *Env) {
	if backendPID, alive := daemon.LiveAt(env.BackendPIDPath()); alive {
		_ = daemon.KillPID(backendPID)
	}
	if frontendPID, alive := daemon.LiveAt(env.FrontendPIDPath()); alive {
		_ = daemon.KillPID(frontendPID)
	}
	if daemonPID, alive := daemon.LiveAt(env.DaemonPIDPath()); alive {
		_ = daemon.KillPID(daemonPID)
	}
	_ = daemon.RemovePID(env.BackendPIDPath())
	_ = daemon.RemovePID(env.FrontendPIDPath())
	_ = daemon.RemovePID(env.DaemonPIDPath())
	_ = os.Remove(env.StopSentinelPath())
}
