package commands

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
)

// RunSkills implements `wizard skills ...`. It fronts the exact subcommand
// set `backend/main.py skills` already has -- add/list/update/discard/remove/
// token, backed by the same install machinery the REST routes use -- rather
// than reimplementing "resolve a ref, refuse an executable payload, show the
// contents before installing" a second time in Go. See main.py's own module
// docstring, which names this binary as the thing that was meant to do this.
//
// Stdin is wired through unlike every other command here: `skills add`/
// `skills update` print a skill's full contents and ask for confirmation
// before writing anything (unless `--yes` is given), and that prompt has to
// reach a real terminal to be answered.
func RunSkills(env *Env, args []string) int {
	if !env.VenvExists() {
		fmt.Fprintln(env.Err, "No Python environment found at "+env.VenvDir+"; run `wizard init` first.")
		return 1
	}

	cmd := exec.Command(env.VenvPython(), append([]string{"main.py", "skills"}, args...)...)
	cmd.Dir = env.BackendDir
	cmd.Stdin = os.Stdin
	cmd.Stdout = env.Out
	cmd.Stderr = env.Err
	err := cmd.Run()
	if err == nil {
		return 0
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		// argparse's own usage/error output already went to env.Err; the exit
		// code is the only thing left to forward.
		return exitErr.ExitCode()
	}
	fmt.Fprintf(env.Err, "could not run `%s main.py skills`: %v\n", env.VenvPython(), err)
	return 1
}
