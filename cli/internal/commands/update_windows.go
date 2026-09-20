//go:build windows

package commands

import (
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
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
	// Swap the current junction without ever leaving the install without one:
	// build the replacement first, move the old one aside, put the new one in
	// place, and on any failure put the old one back.
	current := filepath.Join(*installRoot, "current")
	if err := swapCurrentJunction(current, *packageDir); err != nil {
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

// createJunction makes link a directory junction to target. Junctions need no
// elevation (unlike symlinks). The command line is passed through
// SysProcAttr.CmdLine verbatim: exec.Command would escape the quotes around a
// path with a backslash, which cmd.exe does not understand, so mklink received
// mangled paths and this whole step failed.
func createJunction(link, target string) error {
	if strings.ContainsAny(link+target, `"%^&|<>`) {
		return fmt.Errorf("refusing to build a junction for a path containing shell metacharacters")
	}
	cmd := exec.Command("cmd.exe")
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CmdLine:    fmt.Sprintf(`cmd.exe /d /c mklink /J "%s" "%s"`, link, target),
		HideWindow: true,
	}
	if output, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("mklink /J failed: %v: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

// swapCurrentJunction repoints current at target atomically enough that a
// failure at any step leaves the previous junction in place.
func swapCurrentJunction(current, target string) error {
	next, prev := current+".next", current+".prev"
	_ = os.Remove(next)
	_ = os.Remove(prev)
	if err := createJunction(next, target); err != nil {
		return err
	}
	if _, err := os.Lstat(current); err == nil {
		if err := os.Rename(current, prev); err != nil {
			_ = os.Remove(next)
			return err
		}
	}
	if err := os.Rename(next, current); err != nil {
		if _, statErr := os.Lstat(prev); statErr == nil {
			_ = os.Rename(prev, current) // restore the old release pointer
		}
		_ = os.Remove(next)
		return err
	}
	_ = os.Remove(prev) // removes only the reparse point, never the package
	return nil
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
