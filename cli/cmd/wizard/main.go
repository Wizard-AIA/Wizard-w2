// Command wizard is Milestone 8's single static binary: it manages the
// Wizard backend and frontend as a background service, the same way on
// Linux, macOS and Windows. See cli/README.md for build instructions and a
// full subcommand reference.
package main

import (
	"fmt"
	"os"

	"wizard/internal/commands"
	"wizard/internal/compat"
)

const usage = `wizard - manage the Wizard backend and frontend as a background service

Usage:
  wizard init     Configure setup, check prerequisites, and install dependencies.
  wizard start    Launch the backend and frontend in the background.
  wizard stop     Stop them.
  wizard delete   Stop Wizard and delete its user-level data and configuration.
  wizard status   Show what's running (alias: doctor).
  wizard doctor   Same as status.
  wizard attach   Follow the backend/frontend logs live.
  wizard logs     Print log file paths (add --tail N for recent lines).
  wizard update   Update a checkout, or a release install; add --check to only check.
  wizard skills   Install and manage skills (add/list/update/discard/remove/token).
  wizard version  Print this binary's version and compat marker.

Run from inside a Wizard checkout (or any subdirectory of one).
`

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) == 0 {
		fmt.Print(usage)
		return 0
	}

	cmd, rest := args[0], args[1:]
	if cmd == "__apply-update" {
		// Windows only: a detached, already-verified new binary waits for
		// this process to exit before replacing the locked launcher.
		return commands.RunApplyStagedUpdate(rest)
	}

	switch cmd {
	case "-h", "--help", "help":
		fmt.Print(usage)
		return 0
	case "-v", "--version", "version":
		fmt.Printf("wizard CLI %s, backend API compat v%s\n", compat.BuildVersion, compat.CompatAPIVersion)
		return 0
	}

	env, err := commands.NewEnv()
	if err != nil {
		fmt.Fprintf(os.Stderr, "%v\n", err)
		return 1
	}

	switch cmd {
	case "init":
		return commands.RunInit(env, rest)
	case "start":
		return commands.RunStart(env, rest)
	case "stop":
		return commands.RunStop(env, rest)
	case "delete":
		return commands.RunDelete(env, rest)
	case "status", "doctor":
		return commands.RunStatus(env, rest)
	case "attach":
		return commands.RunAttach(env, rest)
	case "logs":
		return commands.RunLogs(env, rest)
	case "update":
		return commands.RunUpdate(env, rest)
	case "skills":
		return commands.RunSkills(env, rest)
	case "__supervise":
		// Hidden: only `wizard start` invokes this, as a detached child of
		// itself. Not part of the documented interface -- see supervise.go.
		return commands.RunSupervise(env)
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n", cmd)
		fmt.Fprint(os.Stderr, usage)
		return 2
	}
}
