package commands

import (
	"os"
	"path/filepath"
	"testing"
)

func TestActivePortsRoundTrip(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "run"), 0o755); err != nil {
		t.Fatal(err)
	}
	env := &Env{RunDir: filepath.Join(dir, "run")}

	if err := saveActivePorts(env, "9000", "4000"); err != nil {
		t.Fatalf("saveActivePorts: %v", err)
	}
	backend, frontend := loadActivePorts(env)
	if backend != "9000" || frontend != "4000" {
		t.Fatalf("got (%q, %q), want (\"9000\", \"4000\")", backend, frontend)
	}
}

func TestActivePortsCreatesRunDir(t *testing.T) {
	env := &Env{RunDir: filepath.Join(t.TempDir(), "run")} // RunDir does not exist yet
	if err := saveActivePorts(env, "9000", "4000"); err != nil {
		t.Fatalf("saveActivePorts: %v", err)
	}
	backend, frontend := loadActivePorts(env)
	if backend != "9000" || frontend != "4000" {
		t.Fatalf("got (%q, %q), want (\"9000\", \"4000\")", backend, frontend)
	}
}

func TestActivePortsFallBackToDefaults(t *testing.T) {
	dir := t.TempDir()
	env := &Env{RunDir: filepath.Join(dir, "run")} // nothing saved, dir does not even exist
	backend, frontend := loadActivePorts(env)
	if backend != DefaultBackendPort || frontend != DefaultFrontendPort {
		t.Fatalf("got (%q, %q), want defaults (%q, %q)", backend, frontend, DefaultBackendPort, DefaultFrontendPort)
	}
}

func TestRecordedBackendPortPrefersWhatStartRecorded(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("WIZARD_BACKEND_PORT", "")
	if got := recordedBackendPort(dir); got != DefaultBackendPort {
		t.Fatalf("with nothing recorded got %q, want the default", got)
	}
	t.Setenv("WIZARD_BACKEND_PORT", "8123")
	if got := recordedBackendPort(dir); got != "8123" {
		t.Fatalf("with only the env override got %q", got)
	}
	if err := saveActivePorts(&Env{RunDir: dir}, "8080", "3001"); err != nil {
		t.Fatal(err)
	}
	if got := recordedBackendPort(dir); got != "8080" {
		t.Fatalf("a recorded --backend-port must win: got %q, want 8080 (doctor probed the wrong port)", got)
	}
}
