package commands

import (
	"bufio"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// RunDelete removes Wizard's user-level state and checkout-local .env. It
// deliberately does not remove the checkout or the CLI binary: those belong
// to git/Homebrew and can be removed independently with their normal tools.
func RunDelete(env *Env, args []string) int {
	fs := flag.NewFlagSet("delete", flag.ContinueOnError)
	yes := fs.Bool("yes", false, "Skip the deletion confirmation prompt.")
	keepEnv := fs.Bool("keep-env", false, "Keep backend/.env in the checkout.")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if fs.NArg() != 0 {
		fmt.Fprintln(env.Err, "wizard delete does not accept positional arguments")
		return 2
	}

	if !safeDeleteTarget(env.ConfigDir) {
		fmt.Fprintf(env.Err, "refusing to delete unsafe Wizard config path %q\n", env.ConfigDir)
		return 1
	}

	if !*yes {
		if !readerIsTerminal(env.In) {
			fmt.Fprintln(env.Err, "refusing unattended deletion; re-run `wizard delete --yes` to confirm")
			return 2
		}
		fmt.Fprintf(env.Out, "This stops Wizard and deletes its config, credentials, connections, skills, logs, and managed venv at:\n  %s\n", env.ConfigDir)
		if !*keepEnv {
			fmt.Fprintf(env.Out, "It also deletes:\n  %s\n", env.BackendEnvPath())
		}
		fmt.Fprint(env.Out, "Continue? [y/N]: ")
		if !confirmDelete(env.In) {
			fmt.Fprintln(env.Out, "Delete cancelled.")
			return 0
		}
	}

	if code := RunStop(env, nil); code != 0 {
		return code
	}
	if err := removeIfExists(env.ConfigDir); err != nil {
		fmt.Fprintf(env.Err, "could not delete Wizard config %q: %v\n", env.ConfigDir, err)
		return 1
	}
	if !*keepEnv {
		if err := removeIfExists(env.BackendEnvPath()); err != nil {
			fmt.Fprintf(env.Err, "could not delete backend/.env: %v\n", err)
			return 1
		}
	}

	fmt.Fprintln(env.Out, "Wizard data deleted. The checkout and CLI remain installed.")
	return 0
}

func confirmDelete(in io.Reader) bool {
	if in == nil {
		in = os.Stdin
	}
	line, err := bufio.NewReader(in).ReadString('\n')
	if err != nil && err != io.EOF {
		return false
	}
	answer := strings.TrimSpace(strings.ToLower(line))
	return answer == "y" || answer == "yes"
}

func removeIfExists(path string) error {
	if _, err := os.Lstat(path); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return err
	}
	return os.RemoveAll(path)
}

// safeDeleteTarget prevents a malformed WIZARD_CONFIG_DIR from turning this
// command into a broad filesystem deletion. The configured path may be any
// non-root, non-home directory, including a temporary directory in tests.
func safeDeleteTarget(path string) bool {
	if strings.TrimSpace(path) == "" {
		return false
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	clean := filepath.Clean(absolute)
	if clean == string(filepath.Separator) || clean == "." {
		return false
	}
	home, err := os.UserHomeDir()
	if err == nil {
		homeAbs, absErr := filepath.Abs(home)
		if absErr == nil && clean == filepath.Clean(homeAbs) {
			return false
		}
	}
	return filepath.Dir(clean) != clean
}
