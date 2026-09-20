package commands

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"strconv"

	"wizard/internal/exitcode"
)

// parseFlags parses args for a subcommand. done is true when the caller must
// return code immediately: after an explicit -h/--help (code 0, usage on
// stdout) or after a flag error (code exitcode.Usage). It replaces the
// per-command `if err := fs.Parse(args); err != nil { return 2 }`, which made
// `wizard <cmd> --help` exit 2.
func parseFlags(env *Env, fs *flag.FlagSet, args []string) (code int, done bool) {
	fs.SetOutput(env.Err)
	for _, a := range args {
		if a == "--" {
			break
		}
		if a == "-h" || a == "-help" || a == "--help" {
			fs.SetOutput(env.Out)
			break
		}
	}
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitcode.OK, true
		}
		return exitcode.Usage, true
	}
	return 0, false
}

// rejectArgs reports an error when a command that takes no positional
// arguments was given some, instead of silently ignoring them.
func rejectArgs(env *Env, name string, args []string) (code int, done bool) {
	if len(args) == 0 {
		return 0, false
	}
	fmt.Fprintf(env.Err, "wizard %s does not accept arguments (got %q). See `wizard %s --help`.\n", name, args[0], name)
	return exitcode.Usage, true
}

// noFlags handles a command that takes neither flags nor arguments: --help
// exits 0, anything else unexpected is a usage error.
func noFlags(env *Env, name string, args []string) (code int, done bool) {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	if code, done := parseFlags(env, fs, args); done {
		return code, true
	}
	return rejectArgs(env, name, fs.Args())
}

// HelpEnv is the minimal Env for printing a subcommand's --help before the
// real environment (which needs the bundled checkout) can be resolved.
func HelpEnv(out, errw io.Writer) *Env { return &Env{Out: out, Err: errw} }

// WantsHelp reports whether args ask for a subcommand's help.
func WantsHelp(args []string) bool {
	for _, a := range args {
		if a == "--" {
			return false
		}
		if a == "-h" || a == "-help" || a == "--help" {
			return true
		}
	}
	return false
}

// validPort reports whether s is a TCP port number in 1..65535.
func validPort(s string) bool {
	n, err := strconv.Atoi(s)
	return err == nil && n >= 1 && n <= 65535
}
