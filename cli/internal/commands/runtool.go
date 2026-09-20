package commands

import (
	"errors"
	"os/exec"
	"runtime"
	"sync"
	"syscall"
)

// Some tools are launchable from a terminal but not by exec(2): pnpm 12 ships a
// placeholder `pnpm` script with no #! line until its own install step runs.
// A shell copes -- POSIX requires it to fall back to interpreting an ENOEXEC
// file as a script -- but Go's exec.Command does not, so `wizard init` died with
// "fork/exec ...: exec format error" for someone whose pnpm works fine by hand.
//
// Rather than call such a tool broken, run it the way the terminal does: through
// `sh`. This is applied only to the tools Wizard drives and only when a direct
// exec is actually refused, so a normal install is unaffected.

var (
	shellFallback   sync.Map // tool name -> bool
	shellFallbackOn = map[string]bool{"pnpm": true, "uv": true}
)

// toolExec returns the command and arguments to run name with args, routing
// through sh when name can only be started by a shell.
func toolExec(name string, args []string) (string, []string) {
	if runtime.GOOS == "windows" || !shellFallbackOn[name] {
		return name, args
	}
	if needsShell(name) {
		path, err := exec.LookPath(name)
		if err != nil {
			return name, args
		}
		// sh -c 'exec "$0" "$@"' <tool> args...  hands $0/$@ through untouched,
		// so nothing in args is ever interpreted by the shell.
		return "sh", append([]string{"-c", `exec "$0" "$@"`, path}, args...)
	}
	return name, args
}

// needsShell reports (and caches) whether a direct exec of name is refused with
// ENOEXEC while name is otherwise present.
func needsShell(name string) bool {
	if v, ok := shellFallback.Load(name); ok {
		return v.(bool)
	}
	need := false
	if _, err := exec.LookPath(name); err == nil {
		if _, runErr := runCommandOutputErr(name, "--version"); runErr != nil && errors.Is(runErr, syscall.ENOEXEC) {
			need = true
		}
	}
	shellFallback.Store(name, need)
	return need
}

// resetShellFallbackCache forgets earlier probes; tests change PATH between runs.
func resetShellFallbackCache() {
	shellFallback.Range(func(k, _ any) bool { shellFallback.Delete(k); return true })
}
