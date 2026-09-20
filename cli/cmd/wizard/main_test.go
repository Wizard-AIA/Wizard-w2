package main

import (
	"path/filepath"
	"testing"

	"wizard/internal/commands"
	"wizard/internal/exitcode"
	"wizard/internal/repo"
)

// A command that is listed in help but not dispatched (or the reverse) is the
// "stale command / undocumented command" class of bug. Every documented
// command must answer --help with exit 0, which also proves it is dispatched.
func TestEveryDocumentedCommandIsDispatchedAndHelpExitsZero(t *testing.T) {
	defer repo.DisableDefaultCandidates()()
	t.Setenv("WIZARD_ROOT", filepath.Join(t.TempDir(), "no-such-checkout"))
	t.Setenv("WIZARD_CONFIG_DIR", filepath.Join(t.TempDir(), "cfg"))
	t.Chdir(t.TempDir())
	for _, c := range commands.Commands {
		if c.Name == "skills" {
			continue // forwards --help to the Python skills CLI, which needs a checkout
		}
		if !isKnown(c.Name) {
			t.Errorf("%s is documented but isKnown says it is not dispatched", c.Name)
		}
		if code := run([]string{c.Name, "--help"}); code != exitcode.OK {
			t.Errorf("wizard %s --help exit = %d, want 0 (and it must work with no checkout)", c.Name, code)
		}
	}
}

func TestGlobalHelpAndVersionExitZero(t *testing.T) {
	for _, args := range [][]string{nil, {"--help"}, {"-h"}, {"help"}, {"--version"}, {"-v"}, {"version"}, {"help", "start"}, {"--no-color", "--help"}} {
		if code := run(args); code != exitcode.OK {
			t.Errorf("wizard %v exit = %d, want 0", args, code)
		}
	}
}

func TestUnknownCommandIsAUsageError(t *testing.T) {
	if code := run([]string{"strat"}); code != exitcode.Usage {
		t.Fatalf("exit = %d, want %d", code, exitcode.Usage)
	}
	if got := commands.SuggestCommand("strat"); got != "start" {
		t.Fatalf("SuggestCommand(strat) = %q, want start", got)
	}
	if got := commands.SuggestCommand("uninstal"); got != "uninstall" {
		t.Fatalf("SuggestCommand(uninstal) = %q, want uninstall", got)
	}
	if got := commands.SuggestCommand("zzzzzzzz"); got != "" {
		t.Fatalf("a wild guess must not be suggested, got %q", got)
	}
}

// Without the bundled files a command that needs them fails with the
// environment exit code and guidance, not a stack trace or a bare "1".
func TestMissingCheckoutIsAnEnvironmentError(t *testing.T) {
	defer repo.DisableDefaultCandidates()()
	t.Setenv("WIZARD_ROOT", filepath.Join(t.TempDir(), "nope"))
	t.Setenv("WIZARD_CONFIG_DIR", filepath.Join(t.TempDir(), "cfg"))
	t.Chdir(t.TempDir())
	if code := run([]string{"status"}); code != exitcode.Environment {
		t.Fatalf("exit = %d, want %d", code, exitcode.Environment)
	}
}
