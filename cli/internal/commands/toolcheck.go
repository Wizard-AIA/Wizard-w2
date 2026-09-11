package commands

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// ToolCheck is what `wizard init`/`wizard doctor` report about one
// prerequisite: whether it was found, what version, and whether that
// version clears the minimum this project needs.
type ToolCheck struct {
	Name        string
	Found       bool
	Path        string
	Version     string
	MinMajor    int
	MinMinor    int
	OK          bool
	InstallHint string
}

var versionPattern = regexp.MustCompile(`(\d+)\.(\d+)`)
var versionedPythonPattern = regexp.MustCompile(`^python3(?:\.\d+|\d+)$`)

// CheckPython tries `python3` then `python` -- the former is the common
// POSIX name, the latter what Windows installs (and what venvs create on
// every platform once one exists). Every candidate found on PATH is tried,
// not just the first: on Windows, `python3` commonly resolves to the
// Microsoft Store's "App Execution Alias" stub, which is a real, findable
// executable that prints a redirect notice instead of a version -- stopping
// at the first *found* name would report Python missing/broken on a machine
// where `python` itself is a perfectly good 3.12+.
func CheckPython(minMajor, minMinor int) ToolCheck {
	var best ToolCheck
	haveBest := false
	for _, name := range []string{"python3", "python"} {
		path, err := exec.LookPath(name)
		if err != nil {
			continue
		}
		c := checkPythonCandidate(name, path, minMajor, minMinor, "--version")
		if c.OK {
			return c
		}
		if !haveBest {
			best, haveBest = c, true
		}
	}
	for _, path := range versionedPythonCandidates() {
		c := checkPythonCandidate(path, path, minMajor, minMinor, "--version")
		if c.OK {
			return c
		}
		if !haveBest {
			best, haveBest = c, true
		}
	}
	for _, path := range platformPythonCandidates() {
		c := checkPythonCandidate(path, path, minMajor, minMinor, "--version")
		if c.OK {
			return c
		}
		if !haveBest {
			best, haveBest = c, true
		}
	}
	if runtime.GOOS == "windows" {
		// The Windows Store App Execution Alias can occupy python3.exe while
		// the real interpreter is available through the Python launcher. Try
		// that launcher before reporting the alias as the best candidate.
		if launcherPath, err := exec.LookPath("py"); err == nil {
			c := checkPythonCandidate("py", launcherPath, minMajor, minMinor, "-3", "--version")
			if c.OK {
				if interpreter := pythonLauncherInterpreter(); interpreter != "" {
					c.Path = interpreter
				}
				return c
			}
			if !haveBest {
				best, haveBest = c, true
			}
		}
	}
	if haveBest {
		return best
	}
	return ToolCheck{Name: "Python", Found: false, MinMajor: minMajor, MinMinor: minMinor, InstallHint: pythonInstallHint()}
}

// versionedPythonCandidates finds interpreters such as python3.14 when a
// platform does not provide a generic python3 symlink. It only scans the
// process PATH; Windows-specific install roots are handled separately by
// platformPythonCandidates.
func versionedPythonCandidates() []string {
	candidates := make([]string, 0, 4)
	for _, directory := range filepath.SplitList(os.Getenv("PATH")) {
		if directory == "" {
			continue
		}
		matches, _ := filepath.Glob(filepath.Join(directory, "python3*"))
		for _, path := range matches {
			info, err := os.Stat(path)
			if err != nil || !info.Mode().IsRegular() {
				continue
			}
			name := strings.TrimSuffix(strings.ToLower(filepath.Base(path)), ".exe")
			if versionedPythonPattern.MatchString(name) {
				candidates = append(candidates, path)
			}
		}
	}
	return candidates
}

func checkPythonCandidate(name, path string, minMajor, minMinor int, args ...string) ToolCheck {
	version, parsed := parseVersion(runVersion(name, args...))
	return finishCheck(ToolCheck{
		Name: "Python", Found: true, Path: path, Version: version,
		MinMajor: minMajor, MinMinor: minMinor,
		InstallHint: pythonInstallHint(),
	}, parsed, version, minMajor, minMinor)
}

// CheckNode looks for `node` on PATH.
func CheckNode(minMajor int) ToolCheck {
	path, err := exec.LookPath("node")
	if err != nil {
		return ToolCheck{Name: "Node.js", Found: false, MinMajor: minMajor, InstallHint: nodeInstallHint()}
	}
	version, ok := parseVersion(runVersion("node", "--version"))
	return finishCheck(ToolCheck{
		Name: "Node.js", Found: true, Path: path, Version: version,
		MinMajor: minMajor, InstallHint: nodeInstallHint(),
	}, ok, version, minMajor, 0)
}

// CheckOllama only reports presence -- it is optional, and its version
// scheme is not something MODEL_NAME/WORKER_MODEL_NAME selection depends on.
func CheckOllama() ToolCheck {
	path, err := exec.LookPath("ollama")
	if err != nil {
		return ToolCheck{Name: "Ollama", Found: false}
	}
	return ToolCheck{Name: "Ollama", Found: true, Path: path, OK: true}
}

// CheckUV and CheckPnpm are presence-only, like CheckOllama: installDependencies
// shells out to whatever `uv`/`pnpm` a user has, and there is no minimum this
// project pins against -- only Python 3.12 and Node 20 have a real version floor.
func CheckUV() ToolCheck {
	path, err := exec.LookPath("uv")
	if err != nil {
		return ToolCheck{Name: "uv", Found: false, InstallHint: uvInstallHint()}
	}
	version, _ := parseVersion(runVersion("uv", "--version"))
	return ToolCheck{Name: "uv", Found: true, Path: path, Version: version, OK: true, InstallHint: uvInstallHint()}
}

func CheckPnpm() ToolCheck {
	path, err := exec.LookPath("pnpm")
	if err != nil {
		return ToolCheck{Name: "pnpm", Found: false, InstallHint: pnpmInstallHint()}
	}
	version, _ := parseVersion(runVersion("pnpm", "--version"))
	return ToolCheck{Name: "pnpm", Found: true, Path: path, Version: version, OK: true, InstallHint: pnpmInstallHint()}
}

// runVersion is bounded so a PATH-resolved executable that hangs (an
// unrelated program shadowing the expected name, a broken wrapper script)
// cannot block `wizard init`/`wizard doctor` from reporting anything at all.
// A timeout is treated the same as unparsable output -- an unknown version,
// not a crash.
func runVersion(name string, args ...string) string {
	return runCommandOutput(name, args...)
}

func runCommandOutput(name string, args ...string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out // some tools (older Python) print --version to stderr
	if err := cmd.Run(); err != nil && ctx.Err() != nil {
		return ""
	}
	return out.String()
}

// pythonLauncherInterpreter resolves the Python launcher to the concrete
// interpreter path that uv should use for the managed venv. Passing py.exe
// itself would let the launcher choose a different interpreter later.
func pythonLauncherInterpreter() string {
	if runtime.GOOS != "windows" {
		return ""
	}
	path := strings.TrimSpace(runCommandOutput("py", "-3", "-c", "import sys; print(sys.executable)"))
	if filepath.IsAbs(path) {
		return filepath.Clean(path)
	}
	return ""
}

func parseVersion(raw string) (string, [2]int) {
	m := versionPattern.FindStringSubmatch(raw)
	if m == nil {
		return "unknown", [2]int{0, 0}
	}
	major, _ := strconv.Atoi(m[1])
	minor, _ := strconv.Atoi(m[2])
	return m[1] + "." + m[2], [2]int{major, minor}
}

func finishCheck(c ToolCheck, parsed [2]int, version string, minMajor, minMinor int) ToolCheck {
	c.Version = version
	c.OK = parsed[0] > minMajor || (parsed[0] == minMajor && parsed[1] >= minMinor)
	return c
}

func pythonInstallHint() string {
	switch runtime.GOOS {
	case "windows":
		return "winget install Python.Python.3.12  (or download from https://python.org)"
	case "darwin":
		return "brew install python@3.12"
	default:
		return "sudo apt install python3.12  (or your distribution's equivalent)"
	}
}

func nodeInstallHint() string {
	switch runtime.GOOS {
	case "windows":
		return "winget install OpenJS.NodeJS.LTS"
	case "darwin":
		return "brew install node@20"
	default:
		return "use your distribution's Node 20+ package, or https://nodejs.org"
	}
}

func uvInstallHint() string {
	switch runtime.GOOS {
	case "windows":
		return `winget install astral-sh.uv  (or: powershell -c "irm https://astral.sh/uv/install.ps1 | iex")`
	case "darwin":
		return "brew install uv"
	default:
		return "curl -LsSf https://astral.sh/uv/install.sh | sh"
	}
}

func pnpmInstallHint() string {
	switch runtime.GOOS {
	case "windows":
		return "winget install pnpm.pnpm  (or: corepack enable && corepack prepare pnpm@latest --activate)"
	case "darwin":
		return "brew install pnpm"
	default:
		return "corepack enable && corepack prepare pnpm@latest --activate"
	}
}
