package commands

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"wizard/internal/compat"
	"wizard/internal/daemon"
	"wizard/internal/exitcode"
	"wizard/internal/healthcheck"
)

// RunStart implements `wizard start`. It re-execs this same binary into a
// detached, hidden `__supervise` process (see supervise.go) so the
// supervision loop survives this command returning, then waits here in the
// foreground until the backend answers or a timeout/version-mismatch means
// it should not.
func RunStart(env *Env, args []string) int {
	fs := flag.NewFlagSet("start", flag.ContinueOnError)
	backendPortFlag := fs.String("backend-port", "", "Override the backend port (default 8000).")
	frontendPortFlag := fs.String("frontend-port", "", "Override the frontend port (default 3000).")
	noBrowser := fs.Bool("no-browser", false, "Do not open a browser once healthy.")
	timeoutSeconds := fs.Int("timeout", 90, "Seconds to wait for the backend to become healthy.")
	if code, done := parseFlags(env, fs, args); done {
		return code
	}
	if code, done := rejectArgs(env, "start", fs.Args()); done {
		return code
	}
	for name, value := range map[string]string{"--backend-port": *backendPortFlag, "--frontend-port": *frontendPortFlag} {
		if value != "" && !validPort(value) {
			fmt.Fprintf(env.Err, "invalid %s %q: must be a port number from 1 to 65535\n", name, value)
			return exitcode.Usage
		}
	}
	if *timeoutSeconds < 1 {
		fmt.Fprintf(env.Err, "invalid --timeout %d: must be at least 1 second\n", *timeoutSeconds)
		return exitcode.Usage
	}

	if pid, alive := daemon.LiveAt(env.DaemonPIDPath()); alive {
		fmt.Fprintf(env.Err, "wizard is already running (daemon pid %d). Use `wizard status` or `wizard stop`.\n", pid)
		return 1
	}

	// A tool installed by `wizard init` may live outside the PATH this shell
	// was started with (a keg-only Homebrew formula, a user-level pnpm). Extend
	// PATH here so the detached supervisor, which inherits this environment,
	// can start `node`.
	refreshToolPath()

	if !env.VenvExists() {
		fmt.Fprintln(env.Err, "No Python environment found. Run `wizard init` first.")
		return 1
	}
	if _, err := os.Stat(env.FrontendDir + "/.next/standalone/server.js"); err != nil {
		fmt.Fprintln(env.Err, "No frontend build found. Run `wizard init` first.")
		return 1
	}

	// Resolved through the same backendPort()/frontendPort() helpers
	// RunSupervise uses, and always exported -- otherwise a
	// WIZARD_BACKEND_PORT set in the environment (but no --backend-port
	// flag) would be honored by the supervisor and ignored here, and start
	// would poll the wrong URL and report a false health timeout.
	resolvedBackendPort := firstNonEmpty(*backendPortFlag, backendPort())
	resolvedFrontendPort := firstNonEmpty(*frontendPortFlag, frontendPort())
	extraEnv := []string{
		"WIZARD_BACKEND_PORT=" + resolvedBackendPort,
		"WIZARD_FRONTEND_PORT=" + resolvedFrontendPort,
	}

	self, err := os.Executable()
	if err != nil {
		fmt.Fprintf(env.Err, "Could not resolve this binary's own path: %v\n", err)
		return 1
	}

	fmt.Fprintln(env.Out, "Starting the backend and frontend in the background...")
	pid, err := daemon.StartDetached(self, []string{"__supervise"}, env.RepoRoot, append(os.Environ(), extraEnv...), env.DaemonLogPath())
	if err != nil {
		fmt.Fprintf(env.Err, "Could not start the daemon: %v\n", err)
		return 1
	}
	if err := daemon.WritePID(env.DaemonPIDPath(), pid); err != nil {
		fmt.Fprintf(env.Err, "Could not record the daemon pid: %v\n", err)
		// The supervisor is running detached with no pid file recorded for
		// it -- left alone it would be an orphan `wizard stop` can never
		// find, still holding both ports.
		if killErr := daemon.KillPID(pid); killErr != nil {
			fmt.Fprintf(env.Err, "Could not stop the orphaned supervisor (pid %d): %v\n", pid, killErr)
		}
		return 1
	}

	if err := saveActivePorts(env, resolvedBackendPort, resolvedFrontendPort); err != nil {
		fmt.Fprintf(env.Out, "(could not record the active ports for `wizard status`: %v)\n", err)
	}
	backendURL := "http://127.0.0.1:" + resolvedBackendPort
	frontendURL := "http://127.0.0.1:" + resolvedFrontendPort

	fmt.Fprintf(env.Out, "Waiting for the backend at %s to become healthy...\n", backendURL)
	client := healthcheck.NewClient(backendURL)
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(*timeoutSeconds)*time.Second)
	defer cancel()
	health, err := client.WaitHealthy(ctx, time.Duration(*timeoutSeconds)*time.Second, 500*time.Millisecond)
	if err != nil {
		fmt.Fprintf(env.Err, "The backend did not become healthy in time: %v\n", err)
		fmt.Fprintf(env.Err, "Logs: %s and %s\n", env.BackendLogPath(), env.FrontendLogPath())
		requestStop(env)
		return 1
	}

	if mismatched, err := compat.Mismatch(health.Version); err == nil && mismatched {
		fmt.Fprintf(env.Err,
			"This wizard binary (built for backend API v%s) does not match the running backend (v%s).\n"+
				"Run `wizard update`, or rebuild the CLI against this checkout, before starting.\n",
			compat.CompatAPIVersion, health.Version)
		requestStop(env)
		return 1
	}

	fmt.Fprintf(env.Out, "\nBackend:  %s\n", backendURL)
	fmt.Fprintf(env.Out, "Frontend: %s\n", frontendURL)
	fmt.Fprintf(env.Out, "Logs:     %s\n", env.LogsDir)

	if !*noBrowser {
		if err := openBrowser(frontendURL); err != nil {
			fmt.Fprintf(env.Out, "(could not open a browser automatically: %v)\n", err)
		}
	}
	return 0
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

// requestStop is start's own cleanup path when it must not leave a
// half-healthy or mismatched pair running unattended: it writes the same
// stop sentinel `wizard stop` does and gives the supervisor a moment to
// notice, without duplicating stop's own polling/force-kill fallback.
func requestStop(env *Env) {
	_ = os.WriteFile(env.StopSentinelPath(), []byte("1"), 0o644)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, alive := daemon.LiveAt(env.DaemonPIDPath()); !alive {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	// The supervisor did not notice in time. Leaving the sentinel behind
	// would make the *next* `wizard start` shut its own supervisor down at
	// the first poll tick, since daemon.Run only clears the crashed marker
	// on a fresh run, not this one.
	_ = os.Remove(env.StopSentinelPath())
	fmt.Fprintln(env.Err, "The supervisor is still running. Run `wizard stop` to force a shutdown.")
}
