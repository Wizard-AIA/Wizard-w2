package commands

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	"wizard/internal/compat"
	"wizard/internal/daemon"
	"wizard/internal/exitcode"
	"wizard/internal/installkind"
)

func currentBuildVersion() string { return compat.BuildVersion }

// RunUpdate checks a release when asked, updates a git checkout when one is
// present, and otherwise updates the managed release installation. This keeps
// source development explicit while making `wizard update` useful to people
// who installed the published CLI.
//
// It never touches files a package manager owns: a Homebrew or Scoop install
// is upgraded by that tool, and `wizard update` says so instead of racing it.
func RunUpdate(env *Env, args []string) int {
	fs := flag.NewFlagSet("update", flag.ContinueOnError)
	checkOnly := fs.Bool("check", false, "Check GitHub Releases and report whether a newer Wizard is available.")
	self := fs.Bool("self", false, "Update a managed release installation instead of a source checkout.")
	if code, done := parseFlags(env, fs, args); done {
		return code
	}
	if code, done := rejectArgs(env, "update", fs.Args()); done {
		return code
	}
	if *checkOnly && *self {
		fmt.Fprintln(env.Err, "--check and --self cannot be used together")
		return exitcode.Usage
	}
	if *checkOnly {
		return runReleaseCheck(env)
	}

	exe, _ := executablePath()
	info := installkind.Detect(exe, env.RepoRoot)
	env.adoptInstall(info)
	if info.Kind.PackageManaged() {
		fmt.Fprintf(env.Err, "Wizard was installed with %s, which owns its files, so it must upgrade them.\n\n  %s\n\n", info.Kind, info.Kind.UpgradeCommand())
		fmt.Fprintln(env.Err, "Then run `wizard init` to rebuild the dependencies for the new version.")
		return exitcode.Environment
	}
	if *self {
		return runReleaseUpdate(env)
	}
	if info.Kind == installkind.Checkout || isGitCheckout(env.RepoRoot) {
		return runCheckoutUpdate(env)
	}
	return runReleaseUpdate(env)
}

func isGitCheckout(root string) bool {
	return exec.Command("git", "-C", root, "rev-parse", "--is-inside-work-tree").Run() == nil
}

// networkCode maps a release-service failure to its exit code.
func networkCode(err error) int {
	if errors.Is(err, errNetwork) {
		return exitcode.Network
	}
	return exitcode.Failure
}

func runReleaseCheck(env *Env) int {
	release, available, err := releaseCheck(context.Background())
	if err != nil {
		fmt.Fprintf(env.Err, "Could not check for a newer Wizard release: %v\n", err)
		if errors.Is(err, errNetwork) {
			fmt.Fprintln(env.Err, "Check your connection, or set HTTPS_PROXY if you are behind a proxy.")
		}
		return networkCode(err)
	}
	fmt.Fprintf(env.Out, "Installed wizard version: %s\n", currentBuildVersion())
	fmt.Fprintf(env.Out, "Latest Wizard release: %s\n", release.TagName)
	if available {
		fmt.Fprintf(env.Out, "A new version is available. Run `wizard update` to install %s.\n", release.TagName)
	} else {
		fmt.Fprintln(env.Out, "Wizard is up to date.")
	}
	return exitcode.OK
}

// restartGuard brings back a service that an update stopped when the update
// then fails, so a failed update never leaves a working install stopped.
type restartGuard struct {
	env        *Env
	wasRunning bool
	done       bool
}

// fail restarts the previous service (once) and returns code.
func (g *restartGuard) fail(code int) int {
	if g.wasRunning && !g.done {
		g.done = true
		backend, frontend := loadActivePorts(g.env)
		fmt.Fprintln(g.env.Out, "Restarting the previous version...")
		_ = RunStart(g.env, []string{"--backend-port", backend, "--frontend-port", frontend, "--no-browser"})
	}
	return code
}

func runCheckoutUpdate(env *Env) int {
	wasRunning := stopForUpdate(env)
	if wasRunning < 0 {
		return exitcode.Failure
	}
	guard := &restartGuard{env: env, wasRunning: wasRunning > 0}
	fmt.Fprintln(env.Out, "Pulling the latest checkout (fast-forward only)...")
	pull := exec.Command("git", "-C", env.RepoRoot, "pull", "--ff-only")
	pull.Stdout = env.Out
	pull.Stderr = env.Err
	if err := pull.Run(); err != nil {
		fmt.Fprintf(env.Err, "git pull --ff-only failed: %v\nResolve it manually (a merge or a diverged branch needs a decision this command won't make for you), then re-run `wizard update`.\n", err)
		return guard.fail(exitcode.Failure)
	}
	python, ok := updatePrerequisites(env)
	if !ok {
		return guard.fail(exitcode.Environment)
	}
	if err := installDependencies(env, python); err != nil {
		fmt.Fprintf(env.Err, "%v\n", err)
		return guard.fail(exitcode.Failure)
	}
	if !confirmUpdatedAPIVersion(env) {
		return exitcode.Failure // the pair is known to mismatch; do not restart it
	}
	if wasRunning > 0 {
		fmt.Fprintln(env.Out, "\nRestarting...")
		backend, frontend := loadActivePorts(env)
		return RunStart(env, []string{"--backend-port", backend, "--frontend-port", frontend})
	}
	fmt.Fprintln(env.Out, "\nUpdated. Run `wizard start` when you're ready.")
	return exitcode.OK
}

// runReleaseUpdate only activates artifacts that have completed all checks and
// preparation in a private staging directory. It intentionally does not fall
// back to an unchecked download when an older release lacks SHA256SUMS.
func runReleaseUpdate(env *Env) int {
	installRoot, err := managedInstallRoot(env.RepoRoot)
	if err != nil {
		fmt.Fprintf(env.Err, "This Wizard installation cannot update itself: %v\nInstall a release with the official installer, or extract a newer release archive manually.\n", err)
		return exitcode.Environment
	}
	release, available, err := releaseCheck(context.Background())
	if err != nil {
		fmt.Fprintf(env.Err, "Could not check for a newer Wizard release: %v\n", err)
		return networkCode(err)
	}
	fmt.Fprintf(env.Out, "Installed wizard version: %s\nLatest Wizard release: %s\n", currentBuildVersion(), release.TagName)
	if !available {
		fmt.Fprintln(env.Out, "Wizard is up to date.")
		return exitcode.OK
	}
	fmt.Fprintf(env.Out, "A new version is available: %s\n", release.TagName)
	stageDir, packageDir, err := stageReleaseArchive(context.Background(), installRoot, release)
	if err != nil {
		fmt.Fprintf(env.Err, "Could not stage %s; the active installation was not changed: %v\n", release.TagName, err)
		return networkCode(err)
	}
	defer os.RemoveAll(stageDir)
	if err := copyEnvironmentFile(env.RepoRoot, packageDir); err != nil {
		fmt.Fprintf(env.Err, "Could not preserve backend configuration; the active installation was not changed: %v\n", err)
		return exitcode.Failure
	}
	python, ok := updatePrerequisites(env)
	if !ok {
		return exitcode.Environment
	}
	wasRunning := stopForUpdate(env)
	if wasRunning < 0 {
		return exitcode.Failure
	}
	guard := &restartGuard{env: env, wasRunning: wasRunning > 0}
	stagedEnv := *env
	stagedEnv.RepoRoot = packageDir
	stagedEnv.BackendDir = filepath.Join(packageDir, "backend")
	stagedEnv.FrontendDir = filepath.Join(packageDir, "frontend")
	if err := installDependencies(&stagedEnv, python); err != nil {
		fmt.Fprintf(env.Err, "%v\nThe active release was not changed.\n", err)
		return guard.fail(exitcode.Failure)
	}
	backend, frontend := loadActivePorts(env)
	pending, err := activateStagedRelease(installRoot, stageDir, packageDir, release.TagName, wasRunning > 0, backend, frontend)
	if err != nil {
		fmt.Fprintf(env.Err, "Could not activate %s; the current release remains available: %v\n", release.TagName, err)
		return guard.fail(exitcode.Failure)
	}
	if pending {
		fmt.Fprintln(env.Out, "Update staged. The Windows helper will switch to the new release after this command exits.")
		return exitcode.OK
	}
	fmt.Fprintf(env.Out, "Updated to %s. The previous package remains in %s for rollback.\n", release.TagName, installRoot)
	if wasRunning > 0 {
		fmt.Fprintln(env.Out, "Restarting...")
		return startActivatedRelease(env, installRoot)
	}
	fmt.Fprintln(env.Out, "Run `wizard start` when you're ready.")
	return exitcode.OK
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
		fmt.Fprintln(env.Err, "Python is no longer found/new enough; run `wizard doctor` to see what changed.")
		return ToolCheck{}, false
	}
	if uv := CheckUV(); !uv.OK {
		fmt.Fprintln(env.Err, "uv is no longer found or runnable on PATH; run `wizard doctor` to see what changed.")
		return ToolCheck{}, false
	}
	if pnpm := CheckPnpm(); !pnpm.OK {
		fmt.Fprintln(env.Err, "pnpm is no longer found or runnable on PATH; run `wizard doctor` to see what changed.")
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
		return exitcode.Failure
	}
	return exitcode.OK
}
