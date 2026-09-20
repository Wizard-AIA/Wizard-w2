package commands

import (
	"errors"
	"flag"
	"fmt"
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

// validPort reports whether s is a TCP port number in 1..65535.
func validPort(s string) bool {
	n, err := strconv.Atoi(s)
	return err == nil && n >= 1 && n <= 65535
}
