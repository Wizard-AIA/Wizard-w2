//go:build windows

package commands

import (
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"
)

// Windows locks the executable in bin while this process is running. Move the
// verified package now, then run its new executable as a helper which waits
// for this process to exit before replacing the launcher and current junction.
func activateStagedRelease(installRoot, stageDir, packageDir, tag string, restart bool, backend, frontend string) (bool, error) {
	destination := filepath.Join(installRoot, filepath.Base(packageDir))
	if _, err := os.Lstat(destination); err == nil {
		return false, fmt.Errorf("release package %s already exists", filepath.Base(packageDir))
	} else if !os.IsNotExist(err) {
		return false, err
	}
	if err := os.Rename(packageDir, destination); err != nil {
		return false, fmt.Errorf("moving verified release into place: %w", err)
	}
	args := []string{"__apply-update", "--install-root", installRoot, "--package", destination, "--parent-pid", strconv.Itoa(os.Getpid()), "--backend-port", backend, "--frontend-port", frontend}
	if restart {
		args = append(args, "--restart")
	}
	helper := exec.Command(filepath.Join(destination, "cli", "wizard.exe"), args...)
	if err := helper.Start(); err != nil {
		return false, fmt.Errorf("starting update helper: %w", err)
	}
	return true, nil
}

func RunApplyStagedUpdate(args []string) int {
	fs := flag.NewFlagSet("__apply-update", flag.ContinueOnError)
	installRoot := fs.String("install-root", "", "")
	packageDir := fs.String("package", "", "")
	parentPID := fs.Int("parent-pid", 0, "")
	restart := fs.Bool("restart", false, "")
	backend := fs.String("backend-port", DefaultBackendPort, "")
	frontend := fs.String("frontend-port", DefaultFrontendPort, "")
	if fs.Parse(args) != nil || *installRoot == "" || *packageDir == "" || *parentPID <= 0 {
		return 2
	}
	if parent, err := os.FindProcess(*parentPID); err == nil {
		_, _ = parent.Wait()
	} else {
		time.Sleep(2 * time.Second)
	}
	binDir := filepath.Join(*installRoot, "bin")
	launcher := filepath.Join(binDir, "wizard.exe")
	next := launcher + ".next"
	backup := launcher + ".previous-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	if err := copyFile(filepath.Join(*packageDir, "cli", "wizard.exe"), next); err != nil {
		return 1
	}
	if err := os.Rename(launcher, backup); err != nil {
		return 1
	}
	if err := os.Rename(next, launcher); err != nil {
		_ = os.Rename(backup, launcher)
		return 1
	}
	current := filepath.Join(*installRoot, "current")
	if err := os.Remove(current); err != nil {
		_ = os.Rename(launcher, next)
		_ = os.Rename(backup, launcher)
		return 1
	}
	command := exec.Command("cmd.exe", "/c", "mklink /J \""+current+"\" \""+*packageDir+"\"")
	if output, err := command.CombinedOutput(); err != nil {
		_ = output
		_ = os.Rename(launcher, next)
		_ = os.Rename(backup, launcher)
		return 1
	}
	if *restart {
		start := exec.Command(launcher, "start", "--backend-port", *backend, "--frontend-port", *frontend)
		_ = start.Start()
	}
	return 0
}

func copyFile(source, destination string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = out.ReadFrom(in)
	return err
}
