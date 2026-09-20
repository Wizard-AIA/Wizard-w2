package commands

import (
	"fmt"
	"io"
	"os"
	"sort"

	"wizard/internal/compat"
	"wizard/internal/ui"
)

// commandInfo is the single registry of user-facing commands. Help, the
// "did you mean" suggestion and the dispatch test all read it, so a command
// cannot be listed without being dispatched (see
// TestEveryDocumentedCommandIsDispatchedAndHelpExitsZero in cmd/wizard).
type commandInfo struct {
	Name    string
	Group   string
	Summary string
}

// Commands lists every user-facing command in help order.
var Commands = []commandInfo{
	{"init", "Set up", "Choose a model provider, check prerequisites and install dependencies."},
	{"doctor", "Set up", "Diagnose this installation (--json for scripts, --network for connectivity)."},
	{"update", "Set up", "Update to the latest release (--check only reports)."},
	{"uninstall", "Set up", "Remove Wizard; add --purge to delete your data as well."},
	{"start", "Run", "Launch the backend and frontend in the background."},
	{"stop", "Run", "Stop them."},
	{"status", "Run", "Show what is running and the active configuration."},
	{"logs", "Run", "Print log file locations (--tail N for recent lines)."},
	{"attach", "Run", "Follow the live logs."},
	{"skills", "Manage", "Install and manage skills (add, list, update, discard, remove, token)."},
	{"delete", "Manage", "Stop Wizard and delete its data and configuration; the program stays."},
	{"version", "Info", "Print the version and backend API compatibility."},
}

// PrintHelp writes the top-level help.
func PrintHelp(w io.Writer) {
	p := ui.New(w)
	p.Banner(displayVersion(), "autonomous data analyst workspace")
	p.Section("Usage")
	p.Println("  wizard [--no-color] [--verbose] <command> [flags]")
	p.Println("  wizard help <command>        show a command's flags")

	for _, group := range []string{"Set up", "Run", "Manage", "Info"} {
		p.Section(group)
		var rows [][]string
		for _, c := range Commands {
			if c.Group == group {
				rows = append(rows, []string{c.Name, c.Summary})
			}
		}
		for _, r := range rows {
			p.KV(12, r[0], r[1])
		}
	}

	p.Section("First run")
	p.Println("  wizard init      # pick a provider, install what is missing")
	p.Println("  wizard start     # opens http://localhost:3000")
	p.Println("  wizard doctor    # if anything looks wrong")

	p.Section("Exit codes")
	p.KV(12, "0", "success")
	p.KV(12, "1", "failure")
	p.KV(12, "2", "bad usage or invalid value")
	p.KV(12, "3", "missing dependency or broken installation (see `wizard doctor`)")
	p.KV(12, "4", "network error")

	p.Section("Environment")
	p.KV(20, "WIZARD_ROOT", "directory containing backend/ and frontend/")
	p.KV(20, "WIZARD_CONFIG_DIR", "where config, credentials, logs and the Python environment live")
	p.KV(20, "NO_COLOR", "disable colour (same as --no-color)")
	p.KV(20, "HTTPS_PROXY", "proxy for update checks and model lookups")
	p.Println("\nDocumentation: https://wizardw2.vercel.app/docs")
}

// PrintVersion writes the one-line version. The format is stable: scripts and
// the Homebrew formula's test match "wizard CLI vX.Y.Z".
func PrintVersion(w io.Writer) {
	fmt.Fprintf(w, "wizard CLI %s, backend API compat v%s\n", compat.BuildVersion, compat.CompatAPIVersion)
}

// SuggestCommand returns the closest command name to a mistyped one, or "".
func SuggestCommand(typed string) string {
	best, bestDist := "", 3
	names := make([]string, 0, len(Commands))
	for _, c := range Commands {
		names = append(names, c.Name)
	}
	sort.Strings(names)
	for _, name := range names {
		if d := editDistance(typed, name); d < bestDist {
			best, bestDist = name, d
		}
	}
	return best
}

// editDistance is Levenshtein distance; the inputs are short command names.
func editDistance(a, b string) int {
	prev := make([]int, len(b)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(a); i++ {
		cur := make([]int, len(b)+1)
		cur[0] = i
		for j := 1; j <= len(b); j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			cur[j] = min(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
		}
		prev = cur
	}
	return prev[len(b)]
}

// verbose reports whether --verbose (or WIZARD_VERBOSE) is on, for commands
// that can show underlying error detail.
func verbose() bool { return os.Getenv("WIZARD_VERBOSE") != "" }
