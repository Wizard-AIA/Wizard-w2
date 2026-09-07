package repo

import (
	"os"
	"path/filepath"
	"testing"
)

func makeCheckout(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "backend"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dir, "frontend"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "backend", "main.py"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "frontend", "package.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestRootFromFindsCheckoutAtStart(t *testing.T) {
	dir := makeCheckout(t)
	got, err := RootFrom(dir)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != dir {
		t.Fatalf("got %q, want %q", got, dir)
	}
}

func TestRootFromWalksUpFromSubdirectory(t *testing.T) {
	dir := makeCheckout(t)
	sub := filepath.Join(dir, "backend", "src", "core")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	got, err := RootFrom(sub)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != dir {
		t.Fatalf("got %q, want %q", got, dir)
	}
}

func TestRootFromErrorsOutsideAnyCheckout(t *testing.T) {
	dir := t.TempDir() // no backend/frontend markers
	if _, err := RootFrom(dir); err != ErrNotFound {
		t.Fatalf("got err=%v, want ErrNotFound", err)
	}
}

func TestRootFromRequiresBothMarkers(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "backend"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "backend", "main.py"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	// frontend/package.json deliberately missing.
	if _, err := RootFrom(dir); err != ErrNotFound {
		t.Fatalf("got err=%v, want ErrNotFound with only backend/main.py present", err)
	}
}

func TestRootFromExecutableFindsWindowsStyleInstalledLayout(t *testing.T) {
	installDir := t.TempDir()
	checkout := filepath.Join(installDir, "current")
	if err := os.MkdirAll(filepath.Join(installDir, "bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(checkout, "backend"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(checkout, "frontend"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(checkout, "backend", "main.py"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(checkout, "frontend", "package.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}

	got, err := rootFromExecutable(filepath.Join(installDir, "bin", "wizard.exe"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != checkout {
		t.Fatalf("got %q, want current checkout path %q", got, checkout)
	}
}
