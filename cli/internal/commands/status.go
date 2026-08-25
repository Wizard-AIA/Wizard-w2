package commands

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"time"

	"wizard/internal/daemon"
	"wizard/internal/healthcheck"
)

// RunStatus implements both `wizard status` and `wizard doctor` -- the
// evolution spec lists them as one thing ("status/doctor"), and there is no
// second, deeper check doctor would add that status doesn't already run.
//
// Local checks (pid files, logs, a best-effort EXECUTION_BACKEND read) need
// no running backend. When the backend does answer, GET /api/config is
// rendered as-is -- host sizing, sandbox capability, performance notes and
// the rest already live there (backend/src/api/routes/meta.py); this reuses
// that rather than re-deriving any of it.
func RunStatus(env *Env, args []string) int {
	fmt.Fprintln(env.Out, "wizard status")
	fmt.Fprintln(env.Out, "==============")

	daemonPID, daemonAlive := daemon.LiveAt(env.DaemonPIDPath())
	if daemonAlive {
		fmt.Fprintf(env.Out, "daemon:   running (pid %d)\n", daemonPID)
	} else if _, err := os.Stat(env.CrashedMarkerPath()); err == nil {
		reason, _ := os.ReadFile(env.CrashedMarkerPath())
		fmt.Fprintf(env.Out, "daemon:   stopped unexpectedly -- %s", string(reason))
		fmt.Fprintf(env.Out, "          see %s and %s\n", env.BackendLogPath(), env.FrontendLogPath())
	} else {
		fmt.Fprintln(env.Out, "daemon:   not running (`wizard start` to launch it)")
	}

	printChildStatus(env, "backend", env.BackendPIDPath())
	printChildStatus(env, "frontend", env.FrontendPIDPath())

	fmt.Fprintf(env.Out, "\nconfig dir: %s\n", env.ConfigDir)
	fmt.Fprintf(env.Out, "logs dir:   %s\n", env.LogsDir)
	printLogSize(env, "  backend.log ", env.BackendLogPath())
	printLogSize(env, "  frontend.log", env.FrontendLogPath())
	printLogSize(env, "  daemon.log  ", env.DaemonLogPath())

	provider, providerFound, providerErr := readEnvValue(env.BackendEnvPath(), "API_PROVIDER")
	switch {
	case providerErr != nil:
		provider = fmt.Sprintf("(could not read backend/.env: %v)", providerErr)
	case !providerFound || provider == "":
		provider = "ollama (default)"
	}
	dataMode, dataModeFound, dataModeErr := readEnvValue(env.BackendEnvPath(), "DATA_MODE")
	switch {
	case dataModeErr != nil:
		dataMode = fmt.Sprintf("(could not read backend/.env: %v)", dataModeErr)
	case !dataModeFound || dataMode == "":
		dataMode = "(empty -- derives to local-only, or cloud-only if API_PROVIDER is already a cloud backend)"
	}
	fmt.Fprintf(env.Out, "\nAPI_PROVIDER: %s\n", provider)
	fmt.Fprintf(env.Out, "DATA_MODE:    %s\n", dataMode)

	execBackend, found, err := readEnvValue(env.BackendEnvPath(), "EXECUTION_BACKEND")
	switch {
	case err != nil:
		execBackend = fmt.Sprintf("(could not read backend/.env: %v)", err)
	case !found:
		execBackend = "host (default; no backend/.env override)"
	}
	fmt.Fprintf(env.Out, "EXECUTION_BACKEND: %s\n", execBackend)
	if execBackend == "docker" {
		printDockerReachability(env)
	}

	backendPort, _ := loadActivePorts(env)
	backendURL := "http://127.0.0.1:" + backendPort
	fmt.Fprintf(env.Out, "\nbackend at %s:\n", backendURL)
	printRemoteConfig(env, backendURL)

	return 0
}

func printChildStatus(env *Env, label, pidPath string) {
	if pid, alive := daemon.LiveAt(pidPath); alive {
		fmt.Fprintf(env.Out, "%-9s running (pid %d)\n", label+":", pid)
	} else {
		fmt.Fprintf(env.Out, "%-9s not running\n", label+":")
	}
}

func printLogSize(env *Env, label, path string) {
	info, err := os.Stat(path)
	if err != nil {
		fmt.Fprintf(env.Out, "%s: (none yet)\n", label)
		return
	}
	fmt.Fprintf(env.Out, "%s: %s (%.1f KB)\n", label, path, float64(info.Size())/1024)
}

func printDockerReachability(env *Env) {
	if _, err := exec.LookPath("docker"); err != nil {
		fmt.Fprintln(env.Out, "docker:   not found on PATH")
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := exec.CommandContext(ctx, "docker", "info").Run(); err != nil {
		fmt.Fprintln(env.Out, "docker:   found, but the daemon is not reachable (fallback behavior is whatever the backend's own EXECUTION_BACKEND resolution reports -- see the backend section above)")
		return
	}
	fmt.Fprintln(env.Out, "docker:   reachable")
}

func printRemoteConfig(env *Env, backendURL string) {
	client := healthcheck.NewClient(backendURL)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	cfg, err := client.ServerConfig(ctx)
	if err != nil {
		fmt.Fprintf(env.Out, "  not reachable (%v) -- run `wizard start` if you expected it to be up.\n", err)
		return
	}
	pretty, _ := json.MarshalIndent(cfg, "  ", "  ")
	fmt.Fprintf(env.Out, "  %s\n", pretty)
}
