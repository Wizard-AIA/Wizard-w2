package commands

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func deleteTestEnv(t *testing.T) (*Env, string, string) {
	t.Helper()
	root := t.TempDir()
	backendDir := filepath.Join(root, "backend")
	configDir := filepath.Join(root, "wizard-config")
	if err := os.MkdirAll(backendDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(configDir, "logs"), 0o700); err != nil {
		t.Fatal(err)
	}
	return &Env{
		RepoRoot:   root,
		BackendDir: backendDir,
		ConfigDir:  configDir,
		RunDir:     filepath.Join(configDir, "run"),
		LogsDir:    filepath.Join(configDir, "logs"),
		VenvDir:    filepath.Join(configDir, "venv"),
		Out:        &bytes.Buffer{},
		Err:        &bytes.Buffer{},
	}, configDir, filepath.Join(backendDir, ".env")
}

func TestRunDeleteRequiresConfirmationWithoutTerminal(t *testing.T) {
	env, configDir, envPath := deleteTestEnv(t)
	env.In = strings.NewReader("y\n")
	if err := os.WriteFile(envPath, []byte("API_PROVIDER=ollama\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	if code := RunDelete(env, nil); code != 2 {
		t.Fatalf("RunDelete() = %d, want 2", code)
	}
	if _, err := os.Stat(configDir); err != nil {
		t.Fatalf("config was removed without confirmation: %v", err)
	}
	if _, err := os.Stat(envPath); err != nil {
		t.Fatalf(".env was removed without confirmation: %v", err)
	}
}

func TestRunDeleteWithYesRemovesUserDataAndEnv(t *testing.T) {
	env, configDir, envPath := deleteTestEnv(t)
	if err := os.WriteFile(envPath, []byte("API_PROVIDER=ollama\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(configDir, "credentials.json"), []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}

	if code := RunDelete(env, []string{"--yes"}); code != 0 {
		t.Fatalf("RunDelete() = %d, want 0; err=%s", code, env.Err.(*bytes.Buffer).String())
	}
	if _, err := os.Stat(configDir); !os.IsNotExist(err) {
		t.Fatalf("config still exists or could not be checked: %v", err)
	}
	if _, err := os.Stat(envPath); !os.IsNotExist(err) {
		t.Fatalf(".env still exists or could not be checked: %v", err)
	}
}

func TestRunDeleteKeepEnvPreservesCheckoutConfiguration(t *testing.T) {
	env, configDir, envPath := deleteTestEnv(t)
	if err := os.WriteFile(envPath, []byte("API_PROVIDER=ollama\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	if code := RunDelete(env, []string{"--yes", "--keep-env"}); code != 0 {
		t.Fatalf("RunDelete() = %d, want 0", code)
	}
	if _, err := os.Stat(configDir); !os.IsNotExist(err) {
		t.Fatalf("config still exists or could not be checked: %v", err)
	}
	if _, err := os.Stat(envPath); err != nil {
		t.Fatalf("--keep-env did not preserve .env: %v", err)
	}
}

func TestSafeDeleteTargetRejectsBroadPaths(t *testing.T) {
	if safeDeleteTarget("") || safeDeleteTarget(string(filepath.Separator)) {
		t.Fatal("empty and root paths must not be safe delete targets")
	}
	home, err := os.UserHomeDir()
	if err == nil && safeDeleteTarget(home) {
		t.Fatal("the home directory must not be a safe delete target")
	}
	if !safeDeleteTarget(filepath.Join(t.TempDir(), "wizard")) {
		t.Fatal("a nested Wizard config path should be deletable")
	}
}
