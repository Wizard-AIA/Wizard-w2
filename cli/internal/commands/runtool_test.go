//go:build !windows

package commands

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// shebanglessTool writes an executable that exec(2) refuses (no #! line) but a
// shell runs -- the shape of pnpm 12's placeholder before its install step.
func shebanglessTool(t *testing.T, name, body string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+"/usr/bin:/bin")
	resetShellFallbackCache()
	t.Cleanup(resetShellFallbackCache)
	return dir
}

func TestToolThatOnlyAShellCanStartIsReportedUsableNotBroken(t *testing.T) {
	shebanglessTool(t, "pnpm", "echo 12.5.1\n")
	c := CheckPnpm()
	if !c.Found || !c.OK {
		t.Fatalf("a pnpm that works from a terminal must pass the check: %+v", c)
	}
	if c.Version != "12.5" {
		t.Fatalf("version = %q, want 12.5", c.Version)
	}
}

func TestToolExecRoutesThroughShWithArgumentsUntouched(t *testing.T) {
	dir := shebanglessTool(t, "pnpm", "printf '%s|' \"$@\"\n")
	name, args := toolExec("pnpm", []string{"install", "--frozen-lockfile", "a b; rm -rf x", "$(id)"})
	if name != "sh" {
		t.Fatalf("name = %q, want sh", name)
	}
	if args[0] != "-c" || args[2] != filepath.Join(dir, "pnpm") {
		t.Fatalf("the wrapper must exec the tool's own path: %v", args)
	}

	var out bytes.Buffer
	env := &Env{Out: &out, Err: &out}
	if err := runStreamed(env, t.TempDir(), "pnpm", []string{"install", "a b; echo INJECTED", "$(echo INJECTED)"}); err != nil {
		t.Fatalf("running through the shell fallback failed: %v\n%s", err, out.String())
	}
	got := out.String()
	if !strings.Contains(got, "install|a b; echo INJECTED|$(echo INJECTED)|") {
		t.Fatalf("arguments must reach the tool byte-for-byte:\n%s", got)
	}
	// If a shell had interpreted an argument, `echo INJECTED` would have run and
	// printed a line that is exactly INJECTED.
	for _, line := range strings.Split(got, "\n") {
		if strings.TrimSpace(line) == "INJECTED" {
			t.Fatalf("an argument was interpreted by a shell:\n%s", got)
		}
	}
}

// A normal executable is never wrapped: the fallback is only for tools exec
// itself refuses.
func TestNormalToolIsNotRoutedThroughSh(t *testing.T) {
	shebanglessTool(t, "pnpm", "#!/bin/sh\necho 10.0.0\n")
	if name, _ := toolExec("pnpm", []string{"--version"}); name != "pnpm" {
		t.Fatalf("a directly-executable tool must run directly, got %q", name)
	}
}

func TestToolExecLeavesOtherCommandsAlone(t *testing.T) {
	if name, args := toolExec("git", []string{"status"}); name != "git" || len(args) != 1 {
		t.Fatalf("only the tools Wizard drives are eligible, got %q %v", name, args)
	}
}

func TestGenuinelyBrokenToolIsStillReportedBroken(t *testing.T) {
	shebanglessTool(t, "uv", "exit 3\n") // starts (via sh) but fails
	c := CheckUV()
	if c.OK {
		t.Fatalf("a tool that exits non-zero must not pass: %+v", c)
	}
}
