// Command wizard is Milestone 8's single static binary: it manages the
// Wizard backend and frontend as a background service, the same way on
// Linux, macOS and Windows. See cli/README.md for build instructions and a
// full subcommand reference.
package main

import (
	"errors"
	"fmt"
	"os"

	"wizard/internal/commands"
	"wizard/internal/exitcode"
	"wizard/internal/repo"
)

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	args = consumeGlobalFlags(args)
	if len(args) == 0 {
		commands.PrintHelp(os.Stdout)
		return exitcode.OK
	}

	cmd, rest := args[0], args[1:]
	if cmd == "__apply-update" {
		// Windows only: a detached, already-verified new binary waits for
		// this process to exit before replacing the locked launcher.
		return commands.RunApplyStagedUpdate(rest)
	}

	switch cmd {
	case "-h", "--help":
		commands.PrintHelp(os.Stdout)
		return exitcode.OK
	case "help":
		if len(rest) == 0 {
			commands.PrintHelp(os.Stdout)
			return exitcode.OK
		}
		// `wizard help init` is `wizard init --help`.
		return run(append([]string{rest[0], "--help"}, rest[1:]...))
	case "-v", "--version", "version":
		commands.PrintVersion(os.Stdout)
		return exitcode.OK
	case "doctor":
		// Runs without a checkout: diagnosing a missing one is its job.
		return commands.RunDoctor(os.Stdout, os.Stderr, rest)
	case "channel":
		// Also checkout-free: the installers call it right after installing.
		return commands.RunChannel(os.Stdout, os.Stderr, rest)
	}

	if !isKnown(cmd) {
		fmt.Fprintf(os.Stderr, "wizard: unknown command %q", cmd)
		if s := commands.SuggestCommand(cmd); s != "" {
			fmt.Fprintf(os.Stderr, "; did you mean %q?", s)
		}
		fmt.Fprintln(os.Stderr, "\nRun `wizard --help` to see the commands.")
		return exitcode.Usage
	}

	// `wizard <command> --help` must work even when the bundled files cannot
	// be found; the command's own flag set prints the usage. `skills` forwards
	// its arguments to the backend, which answers its own --help.
	if cmd != "skills" && cmd != "__supervise" && commands.WantsHelp(rest) {
		return runWithEnv(commands.HelpEnv(os.Stdout, os.Stderr), cmd, rest)
	}

	env, err := commands.NewEnv()
	if err != nil {
		return reportEnvError(err)
	}
	return runWithEnv(env, cmd, rest)
}

// runWithEnv dispatches an already-vetted command.
func runWithEnv(env *commands.Env, cmd string, rest []string) int {

	switch cmd {
	case "init":
		return commands.RunInit(env, rest)
	case "start":
		return commands.RunStart(env, rest)
	case "stop":
		return commands.RunStop(env, rest)
	case "delete":
		return commands.RunDelete(env, rest)
	case "uninstall":
		return commands.RunUninstall(env, rest)
	case "status":
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
	}
	return exitcode.Failure // unreachable: isKnown vetted cmd
}

// isKnown reports whether cmd is dispatched below (documented commands plus
// the hidden supervisor entry point).
func isKnown(cmd string) bool {
	if cmd == "__supervise" {
		return true
	}
	for _, c := range commands.Commands {
		if c.Name == cmd {
			return true
		}
	}
	return false
}

// consumeGlobalFlags strips leading --no-color and --verbose. They are only
// recognised before the command so they can never collide with a flag a
// subcommand (or the skills backend it forwards to) defines itself.
func consumeGlobalFlags(args []string) []string {
	for len(args) > 0 {
		switch args[0] {
		case "--no-color":
			os.Setenv("WIZARD_NO_COLOR", "1")
		case "--verbose":
			os.Setenv("WIZARD_VERBOSE", "1")
		default:
			return args
		}
		args = args[1:]
	}
	return args
}

// reportEnvError turns "could not set up the environment" into an actionable
// message rather than a bare error, and the environment exit code.
func reportEnvError(err error) int {
	if errors.Is(err, repo.ErrNotFound) {
		fmt.Fprintln(os.Stderr, "Wizard could not find its files (the backend/ and frontend/ directories).")
		fmt.Fprintln(os.Stderr)
		fmt.Fprintln(os.Stderr, "  - Installed with the installer or a package manager? Open a new terminal and retry;")
		fmt.Fprintln(os.Stderr, "    if it persists, reinstall Wizard.")
		fmt.Fprintln(os.Stderr, "  - Running from a source checkout? cd into it, or set WIZARD_ROOT to its path.")
		fmt.Fprintln(os.Stderr)
		fmt.Fprintln(os.Stderr, "Then run: wizard doctor")
		if os.Getenv("WIZARD_VERBOSE") != "" {
			fmt.Fprintf(os.Stderr, "\ndetail: %v\n", err)
		}
		return exitcode.Environment
	}
	fmt.Fprintf(os.Stderr, "Wizard could not prepare its environment: %v\n\nRun: wizard doctor\n", err)
	return exitcode.Environment
}
