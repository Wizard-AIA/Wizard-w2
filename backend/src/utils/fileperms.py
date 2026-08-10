"""Restricting a config file to the account that owns it.

Extracted from ``core/credentials.py`` when Milestone 4's ``connections.json``
needed the same treatment. Deliberately shared rather than copied: the Windows
half has one non-obvious rule in it -- grant to the SID read from the process
token, never to ``%USERNAME%`` -- and that rule was already got wrong once. A
second copy is a second chance to get it wrong.

Enforced on all three platforms rather than documented on two. Every failure
degrades to a warning and inherited permissions: a config file nobody can write
is a worse outcome than one with default permissions, and it would fail at
exactly the moment someone is trying to save something.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from src.utils.logging import logger


def _icacls(*args: str) -> bool:
    try:
        subprocess.run(  # noqa: S603 - fixed executable, arguments are not user input
            ["icacls", *args], check=True, capture_output=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def current_user_sid() -> str:
    """The SID of the account this process is actually running as.

    Read from the process token via ``whoami`` rather than from ``%USERNAME%``,
    which is an ordinary environment variable and can name someone else entirely
    — on the machine this was written on it read ``Wizard``. Granting to a name
    that is not the running user locks the owner out of their own file.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable, no user input
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=15, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    fields = result.stdout.decode(errors="replace").strip().strip('"').split('","')
    return fields[-1].strip() if len(fields) >= 2 and fields[-1].startswith("S-1-") else ""


def _set_entry_native(path: Path, sid: str) -> bool:
    """``icacls /inheritance:r /grant:r *SID:F``, but through advapi32 directly.

    Minimal and Server Core Windows images can lack ``icacls.exe`` (or have it
    blocked by AppLocker), which previously made ``_icacls`` fail and left the
    file on inherited, often world-readable, permissions with only a log line
    to show for it. This talks to the same security APIs icacls wraps, so the
    restriction still lands when the utility itself is missing.
    """
    import ctypes
    from ctypes import wintypes

    GRANT_ACCESS = 1
    NO_INHERITANCE = 0
    FILE_ALL_ACCESS = 0x1F01FF
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x4
    PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    TRUSTEE_IS_SID = 0
    TRUSTEE_IS_UNKNOWN = 0

    class _TrusteeW(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", ctypes.c_void_p),
            ("MultipleTrusteeOperation", ctypes.c_int),
            ("TrusteeForm", ctypes.c_int),
            ("TrusteeType", ctypes.c_int),
            ("ptstrName", ctypes.c_void_p),
        ]

    class _ExplicitAccessW(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", wintypes.DWORD),
            ("grfAccessMode", ctypes.c_int),
            ("grfInheritance", wintypes.DWORD),
            ("Trustee", _TrusteeW),
        ]

    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return False

    psid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        return False

    try:
        entry = _ExplicitAccessW()
        entry.grfAccessPermissions = FILE_ALL_ACCESS
        entry.grfAccessMode = GRANT_ACCESS
        entry.grfInheritance = NO_INHERITANCE
        entry.Trustee.pMultipleTrustee = None
        entry.Trustee.MultipleTrusteeOperation = 0
        entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
        entry.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
        entry.Trustee.ptstrName = psid.value

        new_acl = ctypes.c_void_p()
        if advapi32.SetEntriesInAclW(1, ctypes.byref(entry), None, ctypes.byref(new_acl)) != 0:
            return False

        try:
            # PROTECTED_DACL_SECURITY_INFORMATION blocks inheritance, matching
            # icacls's ``/inheritance:r``; a bare DACL set would leave the old
            # inherited entries alongside the new one instead of replacing them.
            result = advapi32.SetNamedSecurityInfoW(
                str(path),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                new_acl,
                None,
            )
            return result == 0
        finally:
            kernel32.LocalFree(new_acl)
    finally:
        kernel32.LocalFree(psid)


def _reset_native(path: Path) -> None:
    """``icacls /reset``, but through advapi32 directly. Best-effort rollback."""
    import ctypes

    UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
    DACL_SECURITY_INFORMATION = 0x4
    SE_FILE_OBJECT = 1
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.SetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | UNPROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
        )
    except OSError:
        pass


def _restrict_windows(path: Path, description: str) -> None:
    """Grants the running account sole access. ``os.chmod`` does not touch the ACL here.

    Tries ``icacls`` first, falls back to talking to the Win32 security APIs
    directly when the utility itself is unavailable, and verifies the result
    either way, rolled back if it went wrong.
    """
    sid = current_user_sid()
    if not sid:
        logger.warning(f"Could not identify the running account; {description} keeps inherited permissions")
        return

    restricted_natively = False
    if not _icacls(str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"):
        restricted_natively = _set_entry_native(path, sid)
        if not restricted_natively:
            logger.warning(f"Could not restrict permissions on the {description}", path=str(path))
            return

    if not _is_writable(path):
        if restricted_natively:
            _reset_native(path)
        else:
            _icacls(str(path), "/reset")
        logger.warning(f"Restricting the {description} made it unwritable; inherited permissions restored")


def _is_writable(path: Path) -> bool:
    """Whether the running account can actually write ``path``.

    An actual open, not ``os.access``: on Windows ``os.access(..., os.W_OK)``
    reports the read-only *attribute* and does not consult the ACL, which is the
    only thing that just changed. It would answer "writable" for exactly the
    denied-ACL case this check exists to catch, so the rollback would never fire.
    """
    try:
        with path.open("a"):
            return True
    except OSError:
        return False


def restrict(path: Path, description: str = "config file") -> None:
    """Makes ``path`` readable and writable only by the account that runs this process."""
    if str(sys.platform) == "win32":
        _restrict_windows(path, description)
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        logger.warning(f"Could not restrict permissions on the {description}", path=str(path), error=str(exc))


__all__ = ["current_user_sid", "restrict"]
