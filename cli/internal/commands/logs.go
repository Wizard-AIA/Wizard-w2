package commands

import (
	"bufio"
	"flag"
	"fmt"
	"os"

	"wizard/internal/exitcode"
)

// RunLogs implements `wizard logs`: the one-shot sibling of `wizard attach`.
// With no flags it just prints where the logs live -- `doctor` also reports
// this, but a user piping to another tool wants the bare paths without the
// rest of the status report.
func RunLogs(env *Env, args []string) int {
	fs := flag.NewFlagSet("logs", flag.ContinueOnError)
	tail := fs.Int("tail", 0, "Also print the last N lines of each log.")
	if code, done := parseFlags(env, fs, args); done {
		return code
	}
	if code, done := rejectArgs(env, "logs", fs.Args()); done {
		return code
	}
	if *tail < 0 {
		fmt.Fprintf(env.Err, "invalid --tail %d: must be zero or more\n", *tail)
		return exitcode.Usage
	}

	logs := []struct {
		label string
		path  string
	}{
		{"backend", env.BackendLogPath()},
		{"frontend", env.FrontendLogPath()},
		{"daemon", env.DaemonLogPath()},
	}

	for _, l := range logs {
		fmt.Fprintf(env.Out, "%-8s %s\n", l.label, l.path)
	}

	if *tail > 0 {
		for _, l := range logs {
			fmt.Fprintf(env.Out, "\n==> %s <==\n", l.path)
			lines, err := tailLines(l.path, *tail)
			if err != nil {
				fmt.Fprintf(env.Out, "(not available yet: %v)\n", err)
				continue
			}
			for _, line := range lines {
				fmt.Fprintln(env.Out, line)
			}
		}
	}
	return 0
}

func tailLines(path string, n int) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var all []string
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		all = append(all, scanner.Text())
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	if len(all) <= n {
		return all, nil
	}
	return all[len(all)-n:], nil
}
