package commands

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Default ports match what CLAUDE.md already documents for a manual
// `uvicorn`/`pnpm dev` setup: backend on 8000, frontend on 3000. A
// WIZARD_*_PORT override, set by `wizard start`'s flags before it re-execs
// into the supervisor, lets both sides agree on a non-default port without
// a config file.
const (
	DefaultBackendPort  = "8000"
	DefaultFrontendPort = "3000"
)

func backendPort() string {
	if v := os.Getenv("WIZARD_BACKEND_PORT"); v != "" {
		return v
	}
	return DefaultBackendPort
}

func frontendPort() string {
	if v := os.Getenv("WIZARD_FRONTEND_PORT"); v != "" {
		return v
	}
	return DefaultFrontendPort
}

// portRecord is the small piece of state `wizard start` leaves behind so a
// later `wizard status`/`doctor`/`attach` can find the backend it started
// even when a non-default --backend-port/--frontend-port was used. Nothing
// else in the CLI persists state between invocations; this is the one
// exception, kept deliberately small (two strings) rather than growing into
// a general config file.
type portRecord struct {
	Backend  string `json:"backend"`
	Frontend string `json:"frontend"`
}

func portsFilePath(env *Env) string { return filepath.Join(env.RunDir, "ports.json") }

func saveActivePorts(env *Env, backend, frontend string) error {
	data, err := json.Marshal(portRecord{Backend: backend, Frontend: frontend})
	if err != nil {
		return err
	}
	// RunDir normally already exists (appdir.RunDir creates it), but
	// `wizard start` calls this before the supervisor's own daemon.Run has
	// necessarily created it -- see cli/internal/daemon/supervisor.go.
	if err := os.MkdirAll(env.RunDir, 0o700); err != nil {
		return err
	}
	return os.WriteFile(portsFilePath(env), data, 0o644)
}

// loadActivePorts returns the ports the running (or last-started) daemon
// used, falling back to the defaults if nothing was recorded.
func loadActivePorts(env *Env) (backend, frontend string) {
	data, err := os.ReadFile(portsFilePath(env))
	if err != nil {
		return DefaultBackendPort, DefaultFrontendPort
	}
	var rec portRecord
	if err := json.Unmarshal(data, &rec); err != nil {
		return DefaultBackendPort, DefaultFrontendPort
	}
	return firstNonEmpty(rec.Backend, DefaultBackendPort), firstNonEmpty(rec.Frontend, DefaultFrontendPort)
}
