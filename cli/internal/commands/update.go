package commands

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	"wizard/internal/compat"
	"wizard/internal/daemon"
)

func currentBuildVersion() string { return compat.BuildVersion }

// RunUpdate checks a release when asked, updates a git checkout when one is
// present, and otherwise updates the managed release installation. This keeps
// source development explicit while making `wizard update` useful to people
// who installed the published CLI.
func RunUpdate(env *Env, args []string) int {
	fs := flag.NewFlagSet("update", flag.ContinueOnError)
	fs.SetOutput(env.Err)
	checkOnly := fs.Bool("check", false, "Check GitHub Releases and report whether a newer Wizard is available.")
	self := fs.Bool("self", false, "Update a managed release installation instead of a source checkout.")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *checkOnly && *self {
		fmt.Fprintln(env.Err, "--check and --self cannot be used together")
		return 2
	}
	if *checkOnly {
		return runReleaseCheck(env)
	}
	if *self {
		return runReleaseUpdate(env)
	}
	if isGitCheckout(env.RepoRoot) {
		return runCheckoutUpdate(env)
	}
	return runReleaseUpdate(env)
}

func isGitCheckout(root string) bool {
	return exec.Command("git", "-C", root, "rev-parse", "--is-inside-work-tree").Run() == nil
}

func runReleaseCheck(env *Env) int {
	release, available, err := releaseCheck(context.Background())
	if err != nil {
		fmt.Fprintf(env.Err, "Could not check for a newer Wizard release: %v\n", err)
		return 1
	}
	fmt.Fprintf(env.Out, "Installed wizard version: %s\n", currentBuildVersion())
	fmt.Fprintf(env.Out, "Latest Wizard release: %s\n", release.TagName)
	if available {
		fmt.Fprintf(env.Out, "A new version is available. Run `wizard update` to install %s.\n", release.TagName)
	} else {
		fmt.Fprintln(env.Out, "Wizard is up to date.")
	}
	return 0
}

func runCheckoutUpdate(env *Env) int {
	wasRunning := stopForUpdate(env)
	if wasRunning < 0 {
		return 1
	}
	fmt.Fprintln(env.Out, "Pulling the latest checkout (fast-forward only)...")
	pull := exec.Command("git", "-C", env.RepoRoot, "pull", "--ff-only")
	pull.Stdout = env.Out
	pull.Stderr = env.Err
	if err := pull.Run(); err != nil {
		fmt.Fprintf(env.Err, "git pull --ff-only failed: %v\nResolve it manually (a merge or a diverged branch needs a decision this command won't make for you), then re-run `wizard update`.\n", err)
		return 1
	}
	python, ok := updatePrerequisites(env)
	if !ok {
		return 1
	}
	if err := installDependencies(env, python); err != nil {
		fmt.Fprintf(env.Err, "%v\n", err)
		return 1
	}
	if !confirmUpdatedAPIVersion(env) {
		return 1
	}
	if wasRunning > 0 {
		fmt.Fprintln(env.Out, "\nRestarting...")
		backend, frontend := loadActivePorts(env)
		return RunStart(env, []string{"--backend-port", backend, "--frontend-port", frontend})
	}
	fmt.Fprintln(env.Out, "\nUpdated. Run `wizard start` when you're ready.")
	return 0
}

// runReleaseUpdate only activates artifacts that have completed all checks and
// preparation in a private staging directory. It intentionally does not fall
// back to an unchecked download when an older release lacks SHA256SUMS.
func runReleaseUpdate(env *Env) int {
	installRoot, err := managedInstallRoot(env.RepoRoot)
	if err != nil {
		fmt.Fprintf(env.Err, "This Wizard installation cannot update itself: %v\nInstall a release with the official installer, or extract a newer release archive manually.\n", err)
		return 1
	}
	release, available, err := releaseCheck(context.Background())
	if err != nil {
		fmt.Fprintf(env.Err, "Could not check for a newer Wizard release: %v\n", err)
		return 1
	}
	fmt.Fprintf(env.Out, "Installed wizard version: %s\nLatest Wizard release: %s\n", currentBuildVersion(), release.TagName)
	if !available {
		fmt.Fprintln(env.Out, "Wizard is up to date.")
		return 0
	}
	fmt.Fprintf(env.Out, "A new version is available: %s\n", release.TagName)
	stageDir, packageDir, err := stageReleaseArchive(context.Background(), installRoot, release)
	if err != nil {
		fmt.Fprintf(env.Err, "Could not stage %s; the active installation was not changed: %v\n", release.TagName, err)
		return 1
	}
	defer os.RemoveAll(stageDir)
	if err := copyEnvironmentFile(env.RepoRoot, packageDir); err != nil {
		fmt.Fprintf(env.Err, "Could not preserve backend configuration; the active installation was not changed: %v\n", err)
		return 1
	}
	python, ok := updatePrerequisites(env)
	if !ok {
		return 1
	}
	wasRunning := stopForUpdate(env)
	if wasRunning < 0 {
		return 1
	}
	stagedEnv := *env
	stagedEnv.RepoRoot = packageDir
	stagedEnv.BackendDir = filepath.Join(packageDir, "backend")
	stagedEnv.FrontendDir = filepath.Join(packageDir, "frontend")
	if err := installDependencies(&stagedEnv, python); err != nil {
		fmt.Fprintf(env.Err, "%v\nThe active release was not changed.\n", err)
		if wasRunning > 0 {
			backend, frontend := loadActivePorts(env)
			_ = RunStart(env, []string{"--backend-port", backend, "--frontend-port", frontend})
		}
		return 1
	}
	backend, frontend := loadActivePorts(env)
	pending, err := activateStagedRelease(installRoot, stageDir, packageDir, release.TagName, wasRunning > 0, backend, frontend)
	if err != nil {
		fmt.Fprintf(env.Err, "Could not activate %s; the current release remains available: %v\n", release.TagName, err)
		return 1
	}
	if pending {
		fmt.Fprintln(env.Out, "Update staged. The Windows helper will switch to the new release after this command exits.")
		return 0
	}
	fmt.Fprintf(env.Out, "Updated to %s. The previous package remains in %s for rollback.\n", release.TagName, installRoot)
	if wasRunning > 0 {
		fmt.Fprintln(env.Out, "Restarting...")
		return startActivatedRelease(env, installRoot)
	}
	fmt.Fprintln(env.Out, "Run `wizard start` when you're ready.")
	return 0
}

func stopForUpdate(env *Env) int {
	if _, alive := daemon.LiveAt(env.DaemonPIDPath()); !alive {
		return 0
	}
	fmt.Fprintln(env.Out, "Stopping the running daemon before updating...")
	if code := RunStop(env, nil); code != 0 {
		return -1
	}
	return 1
}

func updatePrerequisites(env *Env) (ToolCheck, bool) {
	python := CheckPython(minPythonMajor, minPythonMinor)
	if !python.OK {
		fmt.Fprintln(env.Err, "Python is no longer found/new enough; run `wizard init` to see what changed.")
		return ToolCheck{}, false
	}
	if uv := CheckUV(); !uv.OK {
		fmt.Fprintln(env.Err, "uv is no longer found on PATH; run `wizard init` to see what changed.")
		return ToolCheck{}, false
	}
	if pnpm := CheckPnpm(); !pnpm.OK {
		fmt.Fprintln(env.Err, "pnpm is no longer found on PATH; run `wizard init` to see what changed.")
		return ToolCheck{}, false
	}
	return python, true
}

func confirmUpdatedAPIVersion(env *Env) bool {
	newVersion, err := readAPIVersionFromSource(env)
	if err != nil {
		fmt.Fprintf(env.Err, "(could not read the backend API version after the update: %v; compatibility is unverified)\n", err)
		return true
	}
	mismatched, cmpErr := compat.Mismatch(newVersion)
	if cmpErr != nil {
		fmt.Fprintf(env.Err, "(could not compare API versions: %v; compatibility is unverified)\n", cmpErr)
		return true
	}
	if mismatched {
		fmt.Fprintf(env.Err, "The updated backend now reports API v%s; this wizard binary is built for v%s.\nRebuild/reinstall the wizard CLI before starting it again.\n", newVersion, compat.CompatAPIVersion)
		return false
	}
	return true
}

func copyEnvironmentFile(fromRoot, toRoot string) error {
	contents, err := os.ReadFile(filepath.Join(fromRoot, "backend", ".env"))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(toRoot, "backend", ".env"), contents, 0o600)
}

func startActivatedRelease(env *Env, installRoot string) int {
	command := exec.Command(filepath.Join(installRoot, "bin", "wizard"+executableSuffix()), "start")
	command.Stdout = env.Out
	command.Stderr = env.Err
	command.Stdin = env.In
	if err := command.Run(); err != nil {
		fmt.Fprintf(env.Err, "The new release was activated but did not restart: %v\nRun `wizard start` after resolving the reported issue.\n", err)
		return 1
	}
	return 0
}
