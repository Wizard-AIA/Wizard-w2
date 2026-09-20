//go:build windows

package commands

import (
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"syscall"
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
// running .exe cannot delete itself, so a detached cmd.exe waits a few seconds
// and removes the install root. CmdLine is set directly because Go's argument
// quoting would backslash-escape the quotes and cmd.exe does not understand that.
func scheduleSelfCleanup(installRoot string) {
	cmd := exec.Command("cmd.exe")
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CmdLine:       fmt.Sprintf(`cmd.exe /d /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "%s"`, installRoot),
		HideWindow:    true,
		CreationFlags: windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS,
	}
	_ = cmd.Start()
}
