package commands

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"wizard/internal/exitcode"
	"wizard/internal/installkind"
	"wizard/internal/ui"
)

// executablePath is the running binary; tests replace it.
var executablePath = os.Executable

// RunUninstall implements `wizard uninstall`.
//
//	wizard uninstall           remove the program; keep your data
//	wizard uninstall --purge   remove everything (alias --all)
//
// It only removes what Wizard's own installer created. A package manager's
// files (Homebrew, Scoop) are never touched -- it prints the command instead --
// and a git checkout or an unrecognised layout is refused, because deleting
// "the directory this binary is in" is exactly the mistake to avoid.
func RunUninstall(env *Env, args []string) int {
	fs := flag.NewFlagSet("uninstall", flag.ContinueOnError)
	purge := fs.Bool("purge", false, "Also delete your data: configuration, API keys, connections, skills, logs, the Python environment and backend/.env.")
	fs.BoolVar(purge, "all", false, "Alias for --purge: remove the entire Wizard system.")
	yes := fs.Bool("yes", false, "Do not ask for confirmation.")
	if code, done := parseFlags(env, fs, args); done {
		return code
	}
	if code, done := rejectArgs(env, "uninstall", fs.Args()); done {
		return code
	}

	exe, _ := executablePath()
	info := installkind.Detect(exe, env.RepoRoot)
	env.adoptInstall(info)
	p := ui.New(env.Out)

	plan, err := buildUninstallPlan(env, info, *purge)
	if err != nil {
		fmt.Fprintf(env.Err, "%v\n", err)
		return exitcode.Environment
	}

	// Nothing Wizard is allowed to remove and nothing to purge: say how to
	// proceed instead of doing something surprising.
	if plan.refusal != "" && !*purge {
		fmt.Fprintf(env.Err, "%s\n", plan.refusal)
		return exitcode.Environment
	}

	p.Banner(displayVersion(), "uninstall")
	p.Section("This will")
	if plan.installRoot != "" {
		p.Check(ui.Info, "Remove program", fmt.Sprintf("%s  (%s)", plan.installRoot, strings.Join(plan.installEntries, ", ")))
		p.Check(ui.Info, "Remove PATH entries", "shell startup files and, on Windows, the user PATH")
	}
	if *purge {
		p.Check(ui.Info, "Delete your data", env.ConfigDir+"  (config, credentials, logs, Python environment)")
		p.Check(ui.Info, "Delete backend/.env", env.BackendEnvPath())
	} else if plan.installRoot != "" {
		p.Check(ui.OK, "Keep your data", env.ConfigDir+"  (re-run the installer to use it again)")
		p.Check(ui.OK, "Back up backend/.env", filepath.Join(env.ConfigDir, "backend.env.backup"))
	}
	if plan.refusal != "" {
		p.Check(ui.Warn, "Not removing program", plan.refusal)
	}
	p.Blank()

	if !*yes {
		if !readerIsTerminal(env.In) {
			fmt.Fprintln(env.Err, "refusing unattended uninstall; re-run with --yes to confirm")
			return exitcode.Usage
		}
		word := "Continue?"
		if *purge {
			word = "This deletes your data and cannot be undone. Continue?"
		}
		fmt.Fprintf(env.Out, "%s [y/N]: ", word)
		if !confirmDelete(bufio.NewReader(env.In)) {
			fmt.Fprintln(env.Out, "Uninstall cancelled.")
			return exitcode.OK
		}
	}

	if code := RunStop(env, nil); code != 0 {
		return code
	}

	// API keys live in backend/.env inside the package directory that is about
	// to go. Keep a copy unless the user asked for everything to be deleted.
	if !*purge && plan.installRoot != "" {
		if err := backupEnvFile(env); err != nil {
			fmt.Fprintf(env.Err, "could not back up backend/.env (%v); nothing was removed\n", err)
			return exitcode.Failure
		}
	}

	if plan.installRoot != "" {
		if home, err := os.UserHomeDir(); err == nil {
			changed, err := removeShellIntegration(home, plan.installRoot)
			if err != nil {
				fmt.Fprintf(env.Err, "warning: could not clean shell startup files: %v\n", err)
			}
			for _, f := range changed {
				p.Check(ui.OK, "Cleaned", f)
			}
		}
		if err := removePersistedPath(filepath.Join(plan.installRoot, "bin"), plan.installRoot); err != nil {
			fmt.Fprintf(env.Err, "warning: could not update the user PATH: %v\n", err)
		}
		removed, deferred, err := removeInstallTree(plan.installRoot)
		if err != nil {
			fmt.Fprintf(env.Err, "%v\n", err)
			return exitcode.Failure
		}
		p.Check(ui.OK, "Removed program", fmt.Sprintf("%d item(s) from %s", len(removed), plan.installRoot))
		if len(deferred) > 0 {
			scheduleSelfCleanup(plan.installRoot)
			p.Check(ui.Info, "Finishing", "the running program is removed as this command exits")
		}
	}

	if *purge {
		if err := deleteUserData(env, false); err != nil {
			fmt.Fprintf(env.Err, "%v\n", err)
			return exitcode.Failure
		}
		p.Check(ui.OK, "Deleted data", env.ConfigDir)
	}

	if cmd := info.Kind.UninstallCommand(); cmd != "" {
		p.Blank()
		p.Println("Wizard's files are managed by " + string(info.Kind) + ". Finish removal with:")
		p.Printf("  %s\n", cmd)
	}
	if !*purge && plan.installRoot != "" {
		p.Blank()
		p.Println("Your data was kept. To delete it too, remove:")
		p.Printf("  %s\n", env.ConfigDir)
	}
	p.Blank()
	p.Println("Done.")
	return exitcode.OK
}

// uninstallPlan is what an uninstall would do, worked out before anything is
// touched so the confirmation can list it.
type uninstallPlan struct {
	installRoot    string   // set only for the release-installer layout
	installEntries []string // the known children that will be removed
	refusal        string   // why the program itself will not be removed
}

func buildUninstallPlan(env *Env, info installkind.Info, purge bool) (uninstallPlan, error) {
	var plan uninstallPlan
	switch info.Kind {
	case installkind.Direct:
		if err := checkSafeInstallRoot(info.InstallRoot); err != nil {
			return plan, err
		}
		plan.installRoot = info.InstallRoot
		plan.installEntries = installEntries(info.InstallRoot)
	case installkind.Homebrew, installkind.Scoop:
		plan.refusal = fmt.Sprintf("Wizard was installed with %s, which owns its files. Run: %s", info.Kind, info.Kind.UninstallCommand())
	case installkind.Checkout:
		plan.refusal = "this is a git checkout, not an installed release; delete the directory yourself when you no longer need it"
	default:
		plan.refusal = "this Wizard was not put here by the official installer, so it will not be removed automatically; delete " + env.RepoRoot + " yourself"
	}
	return plan, nil
}

// installChildren are the only names uninstall will remove inside an install
// root; anything else in there belongs to the user and stays.
func isInstallChild(name string) bool {
	switch {
	case name == "bin", name == "current", name == "env":
		return true
	case strings.HasPrefix(name, "Wizard-v") || strings.HasPrefix(name, "Wizard-dev"):
		return true
	case strings.HasPrefix(name, ".wizard-update-"):
		return true
	}
	return false
}

func installEntries(root string) []string {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	var names []string
	for _, e := range entries {
		if isInstallChild(e.Name()) {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	return names
}

// checkSafeInstallRoot refuses roots that could be a mistake: the filesystem
// root, the home directory, or any directory that contains it. A managed
// layout is only ever <something>/.wizard or a dedicated directory, never
// those.
func checkSafeInstallRoot(root string) error {
	clean := filepath.Clean(root)
	if clean == string(filepath.Separator) || filepath.Dir(clean) == clean {
		return fmt.Errorf("refusing to uninstall from %q", root)
	}
	if home, err := os.UserHomeDir(); err == nil {
		homeClean := filepath.Clean(home)
		if clean == homeClean || strings.HasPrefix(homeClean+string(filepath.Separator), clean+string(filepath.Separator)) {
			return fmt.Errorf("refusing to uninstall from %q: it is your home directory or contains it", root)
		}
	}
	return nil
}

// removeInstallTree removes the known children of an install root and then the
// root itself only if that leaves it empty. It never RemoveAll's the root, so a
// file the user keeps there survives. current is detached first, on its own:
// it is a symlink (or a junction on Windows) and must be unlinked, never
// followed into the package it points at. deferred lists paths that could not
// be removed because they are in use -- on Windows, the running executable.
func removeInstallTree(root string) (removed, deferred []string, err error) {
	if err := checkSafeInstallRoot(root); err != nil {
		return nil, nil, err
	}
	current := filepath.Join(root, "current")
	if _, statErr := os.Lstat(current); statErr == nil {
		if rmErr := os.Remove(current); rmErr != nil {
			return nil, nil, fmt.Errorf("could not remove %s: %w", current, rmErr)
		}
		removed = append(removed, "current")
	}
	entries, readErr := os.ReadDir(root)
	if readErr != nil {
		return removed, nil, fmt.Errorf("reading %s: %w", root, readErr)
	}
	for _, e := range entries {
		if !isInstallChild(e.Name()) || e.Name() == "current" {
			continue
		}
		path := filepath.Join(root, e.Name())
		if rmErr := os.RemoveAll(path); rmErr != nil {
			if inUse(rmErr) {
				deferred = append(deferred, path)
				continue
			}
			return removed, deferred, fmt.Errorf("could not remove %s: %w", path, rmErr)
		}
		removed = append(removed, e.Name())
	}
	if len(deferred) == 0 {
		_ = os.Remove(root) // succeeds only when nothing of the user's is left
	}
	return removed, deferred, nil
}

// envBackupPath is where backend/.env is kept outside the package directory.
func envBackupPath(env *Env) string { return filepath.Join(env.ConfigDir, "backend.env.backup") }

// backupEnvFile copies backend/.env, which holds API keys, into the config
// directory (mode 0600). `wizard init` refreshes it after every configuration,
// and `wizard uninstall` writes it before the package is removed, so an upgrade
// or a reinstall can restore it (see restoreEnvBackup).
func backupEnvFile(env *Env) error {
	data, err := os.ReadFile(env.BackendEnvPath())
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if err := os.MkdirAll(env.ConfigDir, 0o700); err != nil {
		return err
	}
	return os.WriteFile(envBackupPath(env), data, 0o600)
}

// restoreEnvBackup recreates a missing backend/.env from the backup. It never
// overwrites an existing file.
func restoreEnvBackup(env *Env) (bool, error) {
	data, err := os.ReadFile(envBackupPath(env))
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	out, err := os.OpenFile(env.BackendEnvPath(), os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o600)
	if err != nil {
		return false, err
	}
	if _, err := out.Write(data); err != nil {
		_ = out.Close()
		return false, err
	}
	return true, out.Close()
}
