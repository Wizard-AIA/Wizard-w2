package commands

import (
	"fmt"
	"os"
	"os/exec"
)

// runStreamed runs a command with its output connected directly to the
// Env's streams -- used for the long steps of `init`/`update` (uv pip install,
// pnpm install/build) where the user watching progress scroll by is the point,
// unlike the short, captured commands in toolcheck.go.
func runStreamed(env *Env, dir, name string, args []string, extraEnv ...string) error {
	fmt.Fprintf(env.Out, "$ %s %s\n", name, joinArgs(args))
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Stdout = env.Out
	cmd.Stderr = env.Err
	cmd.Env = append(os.Environ(), extraEnv...)
	return cmd.Run()
}

func joinArgs(args []string) string {
	out := ""
	for i, a := range args {
		if i > 0 {
			out += " "
		}
		out += a
	}
	return out
}
