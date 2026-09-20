package commands

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// requiredPrerequisites are the tools needed to build and run Wizard itself.
// Ollama is deliberately not included: it is a model server, not a runtime
// dependency, and cloud-only/LM Studio setups do not need it.
func requiredPrerequisites(python, node, uv, pnpm ToolCheck) []ToolCheck {
	checks := []ToolCheck{python, node, uv, pnpm}
	missing := make([]ToolCheck, 0, len(checks))
	for _, check := range checks {
		if !check.OK {
			missing = append(missing, check)
		}
	}
	return missing
}

func requiresOllama(provider, dataMode, embeddingProvider string) bool {
	if embeddingProvider == "ollama" {
		return true
	}
	return (provider == "" || provider == "ollama") && dataMode != "cloud-only"
}

func askInstallPrerequisites(env *Env, checks []ToolCheck) (bool, error) {
	input := env.In
	if input == nil {
		input = os.Stdin
	}
	reader := bufio.NewReader(input)
	fmt.Fprintln(env.Out, "\nRequired tools are missing or too old:")
	for _, check := range checks {
		fmt.Fprintf(env.Out, "  - %s", check.Name)
		if check.InstallHint != "" {
			fmt.Fprintf(env.Out, " (%s)", check.InstallHint)
		}
		fmt.Fprintln(env.Out)
	}
	choice, err := promptChoiceForInput(env.Out, input, reader,
		"Install these prerequisites now?", []string{"yes", "no"}, "yes")
	if err != nil {
		return false, err
	}
	return choice == "yes", nil
}

func installMissingPrerequisites(env *Env, checks []ToolCheck) error {
	need := map[string]bool{}
	for _, check := range checks {
		need[check.Name] = true
	}

	switch runtime.GOOS {
	case "windows":
		return installWindowsPrerequisites(env, need)
	case "darwin":
		return installBrewPrerequisites(env, need)
	case "linux":
		return installLinuxPrerequisites(env, need)
	default:
		return fmt.Errorf("automatic prerequisite installation is not supported on %s; use the install commands printed above", runtime.GOOS)
	}
}

// What init installs when a prerequisite is MISSING.
//
//  1. The host's own tools always win: anything at or above the minimum
//     passes the check and is used as is, so a machine with Python 3.14 or
//     Node 26 installs nothing.
//  2. The minimum is a floor, not a target. When something is missing, init
//     installs the current release (unversioned, and therefore linked onto
//     PATH), because newer is fine as long as Wizard keeps working; a full
//     init, start and analysis has been verified on Python 3.14 and Node 26.
//  3. Only if the current release cannot be installed does init fall back to the
//     minimum-version package, derived from the same constants the checks use so
//     the two cannot drift.
var (
	pythonMinimum = fmt.Sprintf("%d.%d", minPythonMajor, minPythonMinor)
	// nodeMinimumFormula is versioned, hence keg-only on Homebrew: its bin
	// directory is added to PATH by platformToolPaths, and RunStart refreshes
	// PATH so the service finds it too.
	nodeMinimumFormula = fmt.Sprintf("node@%d", minNodeMajor)
	// wingetPythonFallback is used only if `uv python install` fails; winget has
	// no unversioned Python id.
	wingetPythonFallback = "Python.Python." + pythonMinimum

	// wingetPackages, in install order (uv first: it provisions Python).
	wingetPackages = []struct{ name, id string }{
		{"uv", "astral-sh.uv"},
		{"Node.js", "OpenJS.NodeJS.LTS"},
		{"pnpm", "pnpm.pnpm"},
		{"Ollama", "Ollama.Ollama"},
	}
	// brewFormulae lists each tool's candidates, current release first; the
	// first that installs wins.
	brewFormulae = []struct {
		name     string
		formulae []string
	}{
		{"Python", []string{"python", "python@" + pythonMinimum}},
		{"Node.js", []string{"node", nodeMinimumFormula}},
		{"uv", []string{"uv"}},
		{"pnpm", []string{"pnpm"}},
		{"Ollama", []string{"ollama"}},
	}
)

func installWindowsPrerequisites(env *Env, need map[string]bool) error {
	if _, err := exec.LookPath("winget"); err != nil {
		return fmt.Errorf("winget is not installed; install App Installer from the Microsoft Store, then re-run `wizard init`")
	}
	installPythonAfterUV := need["Python"]
	for _, pkg := range wingetPackages {
		if !need[pkg.name] {
			continue
		}
		args := []string{
			"install", "--id", pkg.id, "--exact", "--source", "winget",
			"--accept-source-agreements", "--accept-package-agreements",
		}
		fmt.Fprintf(env.Out, "\nInstalling %s through winget...\n", pkg.name)
		if err := runStreamed(env, env.RepoRoot, "winget", args); err != nil {
			return fmt.Errorf("installing %s through winget failed: %w", pkg.name, err)
		}
	}
	refreshToolPath()
	if installPythonAfterUV {
		if err := installPythonWithUV(env); err != nil {
			fmt.Fprintf(env.Out, "%v\nFalling back to the minimum supported Python through winget.\n", err)
			args := []string{"install", "--id", wingetPythonFallback, "--exact", "--source", "winget",
				"--accept-source-agreements", "--accept-package-agreements"}
			if werr := runStreamed(env, env.RepoRoot, "winget", args); werr != nil {
				return fmt.Errorf("installing Python through winget failed: %w", werr)
			}
			refreshToolPath()
		}
	}
	return nil
}

// installPythonWithUV provisions Python through uv, which picks the newest
// stable release. Unlike a distribution package it needs no root, no
// version-specific package name (Debian 12 and Ubuntu 22.04 have no
// python3.12), and it leaves the system Python alone. CheckPython finds the
// result through `uv python find`.
func installPythonWithUV(env *Env) error {
	if _, err := exec.LookPath("uv"); err != nil {
		return fmt.Errorf("uv is required to provision Python but was not found on PATH; install uv first (%s)", uvInstallHint())
	}
	fmt.Fprintf(env.Out, "\nInstalling Python with uv (any %d.%d or newer is accepted; uv picks the newest stable)...\n", minPythonMajor, minPythonMinor)
	if err := runStreamed(env, env.RepoRoot, "uv", []string{"python", "install"}); err != nil {
		return fmt.Errorf("installing Python through uv failed: %w", err)
	}
	return nil
}

func installBrewPrerequisites(env *Env, need map[string]bool) error {
	if _, err := exec.LookPath("brew"); err != nil {
		return fmt.Errorf("Homebrew is not installed; install it from https://brew.sh, then re-run `wizard init`")
	}
	// The current (unversioned, linked) formula goes first. Versioned formulae
	// such as node@20 are keg-only -- `node` never reaches PATH, which is why the
	// old `brew install node@20` left init failing its own recheck -- and are
	// deprecated on a schedule (node@20 is disabled on 2026-10-28), so they are
	// only the fallback.
	for _, pkg := range brewFormulae {
		if !need[pkg.name] {
			continue
		}
		var lastErr error
		for i, formula := range pkg.formulae {
			fmt.Fprintf(env.Out, "\nInstalling %s through Homebrew (%s)...\n", pkg.name, formula)
			if lastErr = runStreamed(env, env.RepoRoot, "brew", []string{"install", formula}); lastErr == nil {
				break
			}
			if i+1 < len(pkg.formulae) {
				fmt.Fprintf(env.Out, "brew install %s failed; trying %s.\n", formula, pkg.formulae[i+1])
			}
		}
		if lastErr != nil {
			return fmt.Errorf("installing %s through Homebrew failed: %w", pkg.name, lastErr)
		}
	}
	refreshToolPath()
	return nil
}

func installLinuxPrerequisites(env *Env, need map[string]bool) error {
	if _, err := exec.LookPath("brew"); err == nil {
		return installBrewPrerequisites(env, need)
	}

	packageManager, err := linuxPackageManager()
	if err != nil {
		return err
	}
	// uv first: it provisions Python, so no distribution Python package (and
	// no version-specific package name) is ever needed.
	if need["uv"] {
		fmt.Fprintln(env.Out, "\nInstalling uv with the official installer...")
		if err := runStreamed(env, env.RepoRoot, "sh", []string{"-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"}); err != nil {
			return fmt.Errorf("installing uv failed: %w", err)
		}
		refreshToolPath()
	}
	if need["Python"] {
		if err := installPythonWithUV(env); err != nil {
			return err
		}
	}
	// Node comes from the distribution; npm is only pulled in for pnpm below.
	if need["Node.js"] || need["pnpm"] {
		if err := installLinuxSystemPackages(env, packageManager, need); err != nil {
			return err
		}
	}
	if need["pnpm"] {
		if err := installPnpmUserLevel(env); err != nil {
			return err
		}
	}
	refreshToolPath()
	return nil
}

// installPnpmUserLevel gets pnpm without root. Corepack is the lightest route
// but is absent from some distributions' nodejs packages (Debian, Ubuntu) and
// `corepack enable` writes into Node's own directory, which a normal user
// cannot; pnpm's official standalone installer needs neither.
func installPnpmUserLevel(env *Env) error {
	if _, err := exec.LookPath("corepack"); err == nil {
		fmt.Fprintln(env.Out, "\nActivating pnpm through Corepack...")
		home, _ := os.UserHomeDir()
		bin := filepath.Join(home, ".local", "bin")
		if err := os.MkdirAll(bin, 0o755); err == nil {
			// --install-directory keeps the shims out of Node's root-owned prefix.
			if runStreamed(env, env.RepoRoot, "corepack", []string{"enable", "--install-directory", bin, "pnpm"}) == nil {
				return nil
			}
		}
		fmt.Fprintln(env.Out, "Corepack could not activate pnpm; using pnpm's standalone installer instead.")
	}
	fmt.Fprintln(env.Out, "\nInstalling pnpm with its official installer...")
	if err := runStreamed(env, env.RepoRoot, "sh", []string{"-c", "curl -fsSL https://get.pnpm.io/install.sh | sh -"}); err != nil {
		return fmt.Errorf("installing pnpm failed: %w", err)
	}
	return nil
}

func linuxPackageManager() (string, error) {
	for _, name := range []string{"apt-get", "dnf", "pacman", "apk"} {
		if _, err := exec.LookPath(name); err == nil {
			return name, nil
		}
	}
	return "", fmt.Errorf("no supported Linux package manager found (apt-get, dnf, pacman, or apk); use the install commands printed above")
}

// installLinuxSystemPackages installs Node.js (and npm, which pnpm needs) from
// the distribution. Python is deliberately absent: installPythonWithUV covers
// it for every distribution, at whichever version is current.
func installLinuxSystemPackages(env *Env, manager string, need map[string]bool) error {
	var packages []string
	switch manager {
	case "apt-get":
		if need["Node.js"] || need["pnpm"] {
			packages = append(packages, "nodejs", "npm")
		}
	case "dnf":
		if need["Node.js"] || need["pnpm"] {
			packages = append(packages, "nodejs", "npm")
		}
	case "pacman":
		if need["Node.js"] || need["pnpm"] {
			packages = append(packages, "nodejs", "npm")
		}
	case "apk":
		if need["Node.js"] || need["pnpm"] {
			packages = append(packages, "nodejs", "npm")
		}
	}
	if len(packages) == 0 {
		return nil
	}
	args := []string{}
	command, err := privilegedCommand(manager)
	if err != nil {
		return err
	}
	switch manager {
	case "apt-get", "dnf":
		if manager == "apt-get" {
			fmt.Fprintln(env.Out, "\nUpdating apt package indexes...")
			if err := runStreamed(env, env.RepoRoot, command, []string{"apt-get", "update"}); err != nil {
				return fmt.Errorf("updating apt package indexes failed: %w", err)
			}
		}
		command, err = privilegedCommand(manager)
		if err != nil {
			return err
		}
		args = []string{manager, "install", "-y"}
	case "pacman":
		command, err = privilegedCommand(manager)
		if err != nil {
			return err
		}
		args = []string{"pacman", "-S", "--needed", "--noconfirm"}
	case "apk":
		command, err = privilegedCommand(manager)
		if err != nil {
			return err
		}
		args = []string{"apk", "add"}
	}
	args = append(args, packages...)
	fmt.Fprintf(env.Out, "\nInstalling system prerequisites through %s...\n", manager)
	if err := runStreamed(env, env.RepoRoot, command, args); err != nil {
		return fmt.Errorf("installing system prerequisites through %s failed: %w", manager, err)
	}
	return nil
}

func privilegedCommand(packageManager string) (string, error) {
	if os.Geteuid() == 0 {
		return packageManager, nil
	}
	if _, err := exec.LookPath("sudo"); err == nil {
		return "sudo", nil
	}
	return "", fmt.Errorf("%s requires sudo, but sudo was not found; use the install commands printed above", packageManager)
}

// toolPathHook supplies the extra directories refreshToolPath appends. It is a
// variable so tests can keep the machine they run on (which may have Node, pnpm
// or Homebrew in these places) out of their results.
var toolPathHook = platformToolPaths

// refreshToolPath makes tools installed during this init visible to the same
// process. Package managers update the user's shell startup files, but child
// processes cannot mutate the environment of the already-running wizard.
func refreshToolPath() {
	entries := filepath.SplitList(os.Getenv("PATH"))
	home, _ := os.UserHomeDir()
	entries = append(entries, toolPathHook(home)...)
	seen := make(map[string]bool, len(entries))
	clean := make([]string, 0, len(entries))
	for _, entry := range entries {
		if entry != "" && !seen[entry] {
			seen[entry] = true
			clean = append(clean, entry)
		}
	}
	os.Setenv("PATH", strings.Join(clean, string(os.PathListSeparator)))
}
