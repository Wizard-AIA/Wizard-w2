// Package commands implements each `wizard` subcommand. Every command
// receives an *Env so paths are resolved once, the same way regardless of
// which subcommand is running.
package commands

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"

	"wizard/internal/appdir"
	"wizard/internal/repo"
)

// Env is the resolved set of paths and output streams every command works
// against.
type Env struct {
	RepoRoot    string
	BackendDir  string
	FrontendDir string

	ConfigDir string
	RunDir    string
	LogsDir   string
	VenvDir   string

	Out io.Writer
	Err io.Writer
}

// NewEnv resolves the checkout root (walking up from the working directory,
// see internal/repo) and the platform config directory (see
// internal/appdir), creating the config subdirectories commands need.
func NewEnv() (*Env, error) {
	root, err := repo.Root()
	if err != nil {
		return nil, err
	}

	runDir, err := appdir.RunDir()
	if err != nil {
		return nil, fmt.Errorf("resolving the Wizard config directory: %w", err)
	}
	logsDir, err := appdir.LogsDir()
	if err != nil {
		return nil, fmt.Errorf("resolving the Wizard config directory: %w", err)
	}
	venvDir, err := appdir.VenvDir()
	if err != nil {
		return nil, fmt.Errorf("resolving the Wizard config directory: %w", err)
	}
	configDir, err := appdir.ConfigDir()
	if err != nil {
		return nil, fmt.Errorf("resolving the Wizard config directory: %w", err)
	}

	return &Env{
		RepoRoot:    root,
		BackendDir:  repo.BackendDir(root),
		FrontendDir: repo.FrontendDir(root),
		ConfigDir:   configDir,
		RunDir:      runDir,
		LogsDir:     logsDir,
		VenvDir:     venvDir,
		Out:         os.Stdout,
		Err:         os.Stderr,
	}, nil
}

func (e *Env) DaemonPIDPath() string         { return filepath.Join(e.RunDir, "daemon.pid") }
func (e *Env) BackendPIDPath() string        { return filepath.Join(e.RunDir, "backend.pid") }
func (e *Env) FrontendPIDPath() string       { return filepath.Join(e.RunDir, "frontend.pid") }
func (e *Env) StopSentinelPath() string      { return filepath.Join(e.RunDir, "stop-requested") }
func (e *Env) CrashedMarkerPath() string     { return filepath.Join(e.RunDir, "crashed") }
func (e *Env) DaemonLogPath() string         { return filepath.Join(e.LogsDir, "daemon.log") }
func (e *Env) BackendLogPath() string        { return filepath.Join(e.LogsDir, "backend.log") }
func (e *Env) FrontendLogPath() string       { return filepath.Join(e.LogsDir, "frontend.log") }
func (e *Env) BackendEnvExamplePath() string { return filepath.Join(e.BackendDir, ".env.example") }
func (e *Env) BackendEnvPath() string        { return filepath.Join(e.BackendDir, ".env") }

// VenvPython and VenvUvicorn are executables inside the wizard-managed venv
// -- Scripts\ with a .exe suffix on Windows, bin/ everywhere else, which is
// the one thing about a Python venv layout that is genuinely
// platform-specific. There is no VenvPip: `uv venv` does not seed a pip
// binary into the environment, and installs go through `uv pip install
// --python <VenvPython>` instead (see deps.go).
func (e *Env) VenvPython() string  { return venvExe(e.VenvDir, "python") }
func (e *Env) VenvUvicorn() string { return venvExe(e.VenvDir, "uvicorn") }

func venvExe(venvDir, name string) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(venvDir, "Scripts", name+".exe")
	}
	return filepath.Join(venvDir, "bin", name)
}

func (e *Env) VenvExists() bool {
	_, err := os.Stat(e.VenvPython())
	return err == nil
}
