package commands

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"wizard/internal/exitcode"
	"wizard/internal/repo"
)

// fakeCheckout makes a directory repo.Root accepts and points the CLI at it,
// with an empty PATH so no real Python/Node/uv/pnpm leaks into the result.
func fakeCheckout(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	for _, f := range []string{"backend/main.py", "frontend/package.json"} {
		p := filepath.Join(root, f)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("WIZARD_ROOT", root)
	t.Setenv("WIZARD_CONFIG_DIR", filepath.Join(t.TempDir(), "cfg"))
	t.Setenv("PATH", t.TempDir())
	return root
}

func TestDoctorReportsMissingToolsAsFailWithFixAndExitsEnvironment(t *testing.T) {
	fakeCheckout(t)
	var out, errb bytes.Buffer
	code := RunDoctor(&out, &errb, nil)
	if code != exitcode.Environment {
		t.Fatalf("exit = %d, want %d (missing prerequisites)\n%s", code, exitcode.Environment, out.String())
	}
	got := out.String()
	for _, want := range []string{"[FAIL]  Python", "[FAIL]  Node.js", "[FAIL]  uv", "[FAIL]  pnpm", "->", "Fix the failures above"} {
		if !strings.Contains(got, want) {
			t.Errorf("output missing %q:\n%s", want, got)
		}
	}
}

func TestDoctorJSONIsMachineReadable(t *testing.T) {
	fakeCheckout(t)
	var out, errb bytes.Buffer
	RunDoctor(&out, &errb, []string{"--json"})
	var report doctorReport
	if err := json.Unmarshal(out.Bytes(), &report); err != nil {
		t.Fatalf("--json output is not valid JSON: %v\n%s", err, out.String())
	}
	if report.Platform == "" || len(report.Checks) == 0 || report.Summary.Fail == 0 {
		t.Fatalf("unexpected report: %+v", report)
	}
	seen := map[string]bool{}
	for _, c := range report.Checks {
		if seen[c.ID] {
			t.Errorf("duplicate check id %q", c.ID)
		}
		seen[c.ID] = true
		if c.State == "" {
			t.Errorf("check %q has no status", c.ID)
		}
		if c.State == "fail" && c.Fix == "" {
			t.Errorf("failing check %q must tell the user how to fix it", c.ID)
		}
	}
}

// doctor exists for the case where the bundled checkout is not found, so it
// must not require one.
func TestDoctorWorksWithoutACheckout(t *testing.T) {
	defer repo.DisableDefaultCandidates()()
	t.Setenv("WIZARD_ROOT", filepath.Join(t.TempDir(), "nope"))
	t.Setenv("WIZARD_CONFIG_DIR", filepath.Join(t.TempDir(), "cfg"))
	t.Setenv("PATH", t.TempDir())
	t.Chdir(t.TempDir())
	var out, errb bytes.Buffer
	code := RunDoctor(&out, &errb, nil)
	if code != exitcode.Environment {
		t.Fatalf("exit = %d", code)
	}
	if !strings.Contains(out.String(), "[FAIL]  Wizard files") {
		t.Fatalf("a missing checkout must be reported, not crash:\n%s", out.String())
	}
}

func TestDoctorHelpAndBadArgs(t *testing.T) {
	var out, errb bytes.Buffer
	if code := RunDoctor(&out, &errb, []string{"--help"}); code != exitcode.OK {
		t.Fatalf("--help exit = %d", code)
	}
	if !strings.Contains(out.String(), "-json") {
		t.Fatalf("help should list flags:\n%s", out.String())
	}
	if code := RunDoctor(&out, &errb, []string{"bogus"}); code != exitcode.Usage {
		t.Fatalf("positional arg exit = %d, want usage", code)
	}
}

func TestDoctorDoesNotCreateTheConfigDirectory(t *testing.T) {
	fakeCheckout(t)
	cfg := os.Getenv("WIZARD_CONFIG_DIR")
	RunDoctor(&bytes.Buffer{}, &bytes.Buffer{}, nil)
	if _, err := os.Stat(cfg); err == nil {
		t.Fatal("doctor must be read-only; it created the config directory")
	}
}

func TestRedactProxyHidesCredentials(t *testing.T) {
	if got := redactProxy("http://user:s3cret@proxy.corp:8080"); strings.Contains(got, "s3cret") || strings.Contains(got, "user") || !strings.Contains(got, "proxy.corp:8080") {
		t.Fatalf("redactProxy = %q", got)
	}
	if got := redactProxy("http://proxy.corp:8080"); got != "http://proxy.corp:8080" {
		t.Fatalf("a proxy without credentials must be unchanged, got %q", got)
	}
}

func TestDirWritable(t *testing.T) {
	dir := t.TempDir()
	if err := dirWritable(dir); err != nil {
		t.Fatalf("existing temp dir: %v", err)
	}
	if err := dirWritable(filepath.Join(dir, "a", "b", "c")); err != nil {
		t.Fatalf("missing dir under a writable ancestor: %v", err)
	}
	file := filepath.Join(dir, "f")
	if err := os.WriteFile(file, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := dirWritable(file); err == nil {
		t.Fatal("a file is not a writable directory")
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 1 {
		t.Fatalf("dirWritable left files behind: %v", entries)
	}
}
