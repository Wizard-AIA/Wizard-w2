//go:build windows

package commands

import (
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// removePersistedPath takes the install's bin directory out of the user PATH
// and drops WIZARD_ROOT if it points into this install. It edits the registry
// directly so a REG_EXPAND_SZ Path keeps its type and its unexpanded
// %VARIABLES% -- [Environment]::SetEnvironmentVariable would flatten both.
func removePersistedPath(binDir, installRoot string) error {
	key, err := registry.OpenKey(registry.CURRENT_USER, `Environment`, registry.QUERY_VALUE|registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()

	changed := false
	if raw, valueType, err := key.GetStringValue("Path"); err == nil {
		if updated, did := pathListWithout(raw, binDir); did {
			if valueType == registry.EXPAND_SZ {
				err = key.SetExpandStringValue("Path", updated)
			} else {
				err = key.SetStringValue("Path", updated)
			}
			if err != nil {
				return err
			}
			changed = true
		}
	}
	if root, _, err := key.GetStringValue("WIZARD_ROOT"); err == nil {
		if strings.HasPrefix(normalizePathEntry(root), normalizePathEntry(installRoot)) {
			if err := key.DeleteValue("WIZARD_ROOT"); err != nil {
				return err
			}
			changed = true
		}
	}
	if changed {
		broadcastEnvironmentChange()
	}
	return nil
}

// broadcastEnvironmentChange tells running programs (Explorer, new terminals)
// to re-read the environment, so the PATH edit is visible without a logoff.
func broadcastEnvironmentChange() {
	const (
		hwndBroadcast   = 0xffff
		wmSettingChange = 0x001A
		smtoAbortIfHung = 0x0002
	)
	proc := windows.NewLazySystemDLL("user32.dll").NewProc("SendMessageTimeoutW")
	name, err := windows.UTF16PtrFromString("Environment")
	if err != nil {
		return
	}
	var result uintptr
	_, _, _ = proc.Call(hwndBroadcast, wmSettingChange, 0, uintptr(unsafe.Pointer(name)), smtoAbortIfHung, 5000, uintptr(unsafe.Pointer(&result)))
}

// inUse reports the errors Windows returns for a file that a running process
// holds open -- here, the executable that is performing the uninstall.
func inUse(err error) bool {
	var errno syscall.Errno
	if errors.As(err, &errno) {
		return errno == windows.ERROR_SHARING_VIOLATION || errno == windows.ERROR_ACCESS_DENIED
	}
	return false
}

// scheduleSelfCleanup removes what is left once this process has exited: a
// running .exe cannot delete itself. Use a UTF-16LE encoded PowerShell command
// rather than interpolating an install path into cmd.exe syntax; Windows paths
// can legally contain cmd metacharacters such as &, which must never become
// executable text in an uninstall operation.
func scheduleSelfCleanup(installRoot string) {
	quotedRoot := strings.ReplaceAll(installRoot, "'", "''")
	script := fmt.Sprintf("Start-Sleep -Seconds 3; Remove-Item -LiteralPath '%s' -Recurse -Force -ErrorAction SilentlyContinue", quotedRoot)
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encodePowerShellCommand(script))
	cmd.Dir = os.TempDir()
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS,
	}
	_ = cmd.Start()
}

func encodePowerShellCommand(script string) string {
	chars := utf16.Encode([]rune(script))
	bytes := make([]byte, len(chars)*2)
	for i, char := range chars {
		bytes[2*i] = byte(char)
		bytes[2*i+1] = byte(char >> 8)
	}
	return base64.StdEncoding.EncodeToString(bytes)
}
