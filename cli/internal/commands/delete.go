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
	if code, done := parseFlags(env, fs, args); done {
		return code
	}
	if code, done := rejectArgs(env, "delete", fs.Args()); done {
		return code
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
	if err := deleteUserData(env, *keepEnv); err != nil {
		fmt.Fprintf(env.Err, "%v\n", err)
		return 1
	}

	fmt.Fprintln(env.Out, "Wizard data deleted. The checkout and CLI remain installed.")
	return 0
}

// deleteUserData removes the user-level config directory (credentials,
// connections, skills, logs, the managed venv) and, unless keepEnv, the
// checkout's backend/.env. It stops nothing and prints nothing: `wizard
// delete` and `wizard uninstall --purge` each wrap it with their own prompts
// and messages, and both rely on the safety check here.
func deleteUserData(env *Env, keepEnv bool) error {
	if !safeDeleteTarget(env.ConfigDir) {
		return fmt.Errorf("refusing to delete unsafe Wizard config path %q", env.ConfigDir)
	}
	if err := removeIfExists(env.ConfigDir); err != nil {
		return fmt.Errorf("could not delete Wizard config %q: %v", env.ConfigDir, err)
	}
	if !keepEnv {
		if err := removeIfExists(env.BackendEnvPath()); err != nil {
			return fmt.Errorf("could not delete backend/.env: %v", err)
		}
	}
	return nil
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
	if filepath.Dir(clean) == clean {
		return false
	}
	// os.RemoveAll deliberately does not follow a symlink passed as its final
	// argument, but the operating system resolves symlinked *parents* before
	// RemoveAll sees the path. A user-controlled WIZARD_CONFIG_DIR such as
	// /tmp/link/wizard must never allow deletion outside the intended tree.
	return !hasSymlinkComponent(clean)
}

// hasSymlinkComponent reports whether an existing component of path is a
// symlink. Missing suffixes are fine: config directories are created lazily.
// An unreadable component is treated as unsafe because its type cannot be
// established before a destructive operation.
func hasSymlinkComponent(path string) bool {
	volume := filepath.VolumeName(path)
	remainder := strings.TrimPrefix(path, volume)
	current := volume + string(filepath.Separator)
	for _, component := range strings.Split(remainder, string(filepath.Separator)) {
		if component == "" {
			continue
		}
		current = filepath.Join(current, component)
		info, err := os.Lstat(current)
		if os.IsNotExist(err) {
			return false
		}
		if err != nil {
			return true
		}
		if info.Mode()&os.ModeSymlink != 0 {
			// macOS exposes its normal temporary directories through /var and
			// /tmp symlinks into /private. These are OS-owned aliases, not a
			// caller-controlled escape, and tests as well as real CI commonly
			// receive paths through them. Every other symlinked parent is unsafe.
			if isCanonicalSystemAlias(current) {
				continue
			}
			return true
		}
	}
	return false
}

func isCanonicalSystemAlias(path string) bool {
	clean := filepath.Clean(path)
	var expected string
	switch clean {
	case string(filepath.Separator) + "var":
		expected = string(filepath.Separator) + "private" + string(filepath.Separator) + "var"
	case string(filepath.Separator) + "tmp":
		expected = string(filepath.Separator) + "private" + string(filepath.Separator) + "tmp"
	default:
		return false
	}
	resolved, err := filepath.EvalSymlinks(clean)
	return err == nil && filepath.Clean(resolved) == expected
}
