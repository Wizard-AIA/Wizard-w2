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

func installWindowsPrerequisites(env *Env, need map[string]bool) error {
	if _, err := exec.LookPath("winget"); err != nil {
		return fmt.Errorf("winget is not installed; install App Installer from the Microsoft Store, then re-run `wizard init`")
	}
	packages := []struct {
		name string
		id   string
	}{
		{"Python", "Python.Python.3.12"},
		{"Node.js", "OpenJS.NodeJS.LTS"},
		{"uv", "astral-sh.uv"},
		{"pnpm", "pnpm.pnpm"},
		{"Ollama", "Ollama.Ollama"},
	}
	for _, pkg := range packages {
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
	return nil
}

func installBrewPrerequisites(env *Env, need map[string]bool) error {
	if _, err := exec.LookPath("brew"); err != nil {
		return fmt.Errorf("Homebrew is not installed; install it from https://brew.sh, then re-run `wizard init`")
	}
	packages := []struct {
		name    string
		formula string
	}{
		{"Python", "python@3.12"},
		{"Node.js", "node@20"},
		{"uv", "uv"},
		{"pnpm", "pnpm"},
		{"Ollama", "ollama"},
	}
	for _, pkg := range packages {
		if !need[pkg.name] {
			continue
		}
		fmt.Fprintf(env.Out, "\nInstalling %s through Homebrew...\n", pkg.name)
		if err := runStreamed(env, env.RepoRoot, "brew", []string{"install", pkg.formula}); err != nil {
			return fmt.Errorf("installing %s through Homebrew failed: %w", pkg.name, err)
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
	if need["Python"] || need["Node.js"] || need["pnpm"] {
		if err := installLinuxSystemPackages(env, packageManager, need); err != nil {
			return err
		}
	}
	if need["uv"] {
		fmt.Fprintln(env.Out, "\nInstalling uv with the official installer...")
		if err := runStreamed(env, env.RepoRoot, "sh", []string{"-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"}); err != nil {
			return fmt.Errorf("installing uv failed: %w", err)
		}
	}
	if need["pnpm"] {
		fmt.Fprintln(env.Out, "\nActivating pnpm through Corepack...")
		if err := runStreamed(env, env.RepoRoot, "corepack", []string{"enable"}); err != nil {
			return fmt.Errorf("enabling Corepack failed: %w", err)
		}
		if err := runStreamed(env, env.RepoRoot, "corepack", []string{"prepare", "pnpm@latest", "--activate"}); err != nil {
			return fmt.Errorf("activating pnpm failed: %w", err)
		}
	}
	refreshToolPath()
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

func installLinuxSystemPackages(env *Env, manager string, need map[string]bool) error {
	var packages []string
	switch manager {
	case "apt-get":
		if need["Python"] {
			packages = append(packages, "python3.12", "python3.12-venv")
		}
		if need["Node.js"] || need["pnpm"] {
			packages = append(packages, "nodejs", "npm")
		}
	case "dnf":
		if need["Python"] {
			packages = append(packages, "python3.12")
		}
		if need["Node.js"] || need["pnpm"] {
			packages = append(packages, "nodejs", "npm")
		}
	case "pacman":
		if need["Python"] {
			packages = append(packages, "python")
		}
		if need["Node.js"] || need["pnpm"] {
			packages = append(packages, "nodejs", "npm")
		}
	case "apk":
		if need["Python"] {
			packages = append(packages, "python3")
		}
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

// refreshToolPath makes tools installed during this init visible to the same
// process. Package managers update the user's shell startup files, but child
// processes cannot mutate the environment of the already-running wizard.
func refreshToolPath() {
	entries := filepath.SplitList(os.Getenv("PATH"))
	home, _ := os.UserHomeDir()
	entries = append(entries, platformToolPaths(home)...)
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
