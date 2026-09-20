package commands

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func backupEnv(t *testing.T) (*Env, *bytes.Buffer) {
	t.Helper()
	backendDir := filepath.Join(t.TempDir(), "backend")
	writeBackendEnv(t, backendDir, "")
	_ = os.Remove(filepath.Join(backendDir, ".env"))
	if err := os.WriteFile(filepath.Join(backendDir, ".env.example"), []byte("API_PROVIDER=ollama\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	env := newTestEnv(t, backendDir)
	env.ConfigDir = t.TempDir()
	out := &bytes.Buffer{}
	env.Out, env.Err = out, out
	return env, out
}

// A `brew upgrade` deletes the keg holding backend/.env. The backup init keeps in
// the config directory brings the API keys back on the next init.
func TestEnvSurvivesAPackageReplacement(t *testing.T) {
	env, out := backupEnv(t)
	if err := os.WriteFile(env.BackendEnvPath(), []byte("API_PROVIDER=gemini\nGEMINI_API_KEY=keep-me\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := backupEnvFile(env); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(env.BackendEnvPath()); err != nil { // the new package has no .env
		t.Fatal(err)
	}

	if err := ensureEnvFile(env, false, "", ""); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(env.BackendEnvPath())
	if err != nil || !strings.Contains(string(data), "GEMINI_API_KEY=keep-me") {
		t.Fatalf("the previous configuration was not restored: %q, %v", data, err)
	}
	if !strings.Contains(out.String(), "Restored your previous configuration") {
		t.Fatalf("the user must be told what happened:\n%s", out.String())
	}
}

func TestRestoreNeverOverwritesAnExistingEnv(t *testing.T) {
	env, _ := backupEnv(t)
	if err := os.WriteFile(envBackupPath(env), []byte("OLD=1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(env.BackendEnvPath(), []byte("CURRENT=1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if restored, err := restoreEnvBackup(env); err == nil && restored {
		t.Fatal("restore must refuse to replace an existing backend/.env")
	}
	if data, _ := os.ReadFile(env.BackendEnvPath()); string(data) != "CURRENT=1\n" {
		t.Fatalf("existing configuration was modified: %q", data)
	}
}

func TestWithoutABackupTheExampleFileIsUsed(t *testing.T) {
	env, _ := backupEnv(t)
	if err := ensureEnvFile(env, false, "", ""); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(env.BackendEnvPath()); !strings.Contains(string(data), "API_PROVIDER=ollama") {
		t.Fatalf("expected the example file's content, got %q", data)
	}
}

func TestBackupIsPrivate(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX permission bits are not meaningful on Windows")
	}
	env, _ := backupEnv(t)
	if err := os.WriteFile(env.BackendEnvPath(), []byte("K=v\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := backupEnvFile(env); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(envBackupPath(env))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("backup mode = %v, %v; API keys must be 0600", info.Mode().Perm(), err)
	}
}

func TestBackupOfAMissingEnvIsANoop(t *testing.T) {
	env, _ := backupEnv(t)
	if err := backupEnvFile(env); err != nil {
		t.Fatalf("nothing to back up must not be an error: %v", err)
	}
	if _, err := os.Stat(envBackupPath(env)); err == nil {
		t.Fatal("no backup should be created from nothing")
	}
}
