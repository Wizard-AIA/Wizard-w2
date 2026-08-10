"""Restrictions the sandboxed child applies to itself.

Loaded **by file path** from the generated bootstrap, so it must import nothing
from ``src`` -- stdlib and ctypes only. That is also why all three platforms live
in one module rather than a package: the child gets no import machinery beyond
``spec_from_file_location``.

Enforcement happens in two phases, because the daemon binds a loopback TCP
listener and a filter that denies ``socket()`` cannot be installed before it:

* :func:`apply_policy` runs **before** the daemon starts -- filesystem, memory,
  and no-new-privs.
* :func:`seal_network` runs **after** ``listen()``. ``accept()`` on an
  already-bound descriptor makes no ``socket()`` call, so the connection the
  parent needs survives a filter that refuses to create new ones.

Nothing here raises. A restriction that cannot be applied is reported as
unenforced and the caller decides what that means -- refusing under ``require``
is a policy decision, and policy does not belong in the mechanism.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys


# --------------------------------------------------------------------------- #
# Linux: Landlock (filesystem) and seccomp-bpf (network)
# --------------------------------------------------------------------------- #
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

# Syscall numbers are per-architecture. Only the two the analysis stack actually
# runs on are listed; anything else reports Landlock as unavailable rather than
# issuing a syscall number that means something different there.
LANDLOCK_SYSCALLS = {
    "x86_64": (444, 445, 446),
    "aarch64": (444, 445, 446),
    "arm64": (444, 445, 446),
}

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1

# Access bits, ABI 1 unless noted. The read set and the write set are separated
# so a readable root can be granted without also granting modification.
_A = {
    "execute": 1 << 0,
    "write_file": 1 << 1,
    "read_file": 1 << 2,
    "read_dir": 1 << 3,
    "remove_dir": 1 << 4,
    "remove_file": 1 << 5,
    "make_char": 1 << 6,
    "make_dir": 1 << 7,
    "make_reg": 1 << 8,
    "make_sock": 1 << 9,
    "make_fifo": 1 << 10,
    "make_block": 1 << 11,
    "make_sym": 1 << 12,
    "refer": 1 << 13,  # ABI 2
    "truncate": 1 << 14,  # ABI 3
    "ioctl_dev": 1 << 15,  # ABI 5
}

#: Highest access bit each ABI version understands. Passing a bit the running
#: kernel does not know makes `landlock_create_ruleset` fail outright, so the
#: handled set is masked down to what this kernel admits to supporting.
LANDLOCK_ABI_MASK = {
    1: 0x1FFF,
    2: 0x3FFF,
    3: 0x7FFF,
    4: 0x7FFF,
    5: 0xFFFF,
}

READ_ACCESS = _A["execute"] | _A["read_file"] | _A["read_dir"]
WRITE_ACCESS = (
    READ_ACCESS
    | _A["write_file"]
    | _A["remove_dir"]
    | _A["remove_file"]
    | _A["make_dir"]
    | _A["make_reg"]
    | _A["make_sock"]
    | _A["make_fifo"]
    | _A["make_sym"]
    | _A["refer"]
    | _A["truncate"]
)


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def landlock_abi() -> int:
    """The Landlock ABI this kernel supports, or 0 when it has none."""
    if not sys.platform.startswith("linux"):
        return 0
    numbers = LANDLOCK_SYSCALLS.get(os.uname().machine)
    if numbers is None:
        return 0
    try:
        version = _libc().syscall(numbers[0], None, 0, LANDLOCK_CREATE_RULESET_VERSION)
    except OSError:
        return 0
    return version if version > 0 else 0


def _apply_landlock(policy: dict) -> tuple[bool, str]:
    abi = landlock_abi()
    if abi <= 0:
        return False, "this kernel has no Landlock (5.13+ required)"

    libc = _libc()
    create, add_rule, restrict = LANDLOCK_SYSCALLS[os.uname().machine]
    mask = LANDLOCK_ABI_MASK.get(abi, LANDLOCK_ABI_MASK[5])

    attr = _LandlockRulesetAttr(handled_access_fs=WRITE_ACCESS & mask, handled_access_net=0)
    ruleset = libc.syscall(create, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        return False, f"landlock_create_ruleset failed (errno {ctypes.get_errno()})"

    try:
        for access, roots in (
            (READ_ACCESS & mask, policy.get("readable") or ()),
            (WRITE_ACCESS & mask, policy.get("writable") or ()),
        ):
            for root in roots:
                try:
                    fd = os.open(root, os.O_PATH | os.O_CLOEXEC)  # type: ignore[attr-defined]
                except OSError:
                    # A root that is not there is not a failure: the readable
                    # set is a superset covering several layouts on purpose.
                    continue
                try:
                    rule = _LandlockPathBeneathAttr(allowed_access=access, parent_fd=fd)
                    if libc.syscall(add_rule, ruleset, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0) < 0:
                        return False, f"landlock_add_rule failed for {root} (errno {ctypes.get_errno()})"
                finally:
                    os.close(fd)

        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            return False, "PR_SET_NO_NEW_PRIVS was refused"
        if libc.syscall(restrict, ruleset, 0) < 0:
            return False, f"landlock_restrict_self failed (errno {ctypes.get_errno()})"
    finally:
        os.close(ruleset)

    return True, f"Landlock ABI {abi}"


def _seccomp_deny_inet() -> tuple[bool, str]:
    """Refuses ``socket()`` for the internet families, keeping AF_UNIX.

    A BPF filter cannot dereference a pointer, but ``socket()``'s domain is a
    scalar argument, so this is one of the few network policies seccomp can
    express exactly. Everything else is allowed through, because this is a
    network boundary and not an attempt at a syscall allowlist.
    """
    if not sys.platform.startswith("linux"):
        return False, "seccomp is Linux-only"

    machine = os.uname().machine
    # AUDIT_ARCH values; the filter must refuse to run on anything it was not
    # written for, or the syscall numbers below would mean something else.
    arch = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7, "arm64": 0xC00000B7}.get(machine)
    socket_nr = {"x86_64": 41, "aarch64": 198, "arm64": 198}.get(machine)
    if arch is None or socket_nr is None:
        return False, f"no seccomp filter for {machine}"

    ld, jeq, jmp, ret = 0x20, 0x15, 0x05, 0x06
    kill_arch, allow, errno_eafnosupport = 0x00000000, 0x7FFF0000, 0x00050000 | 97

    af_inet, af_inet6, af_packet, af_netlink = 2, 10, 17, 16

    program = [
        _SockFilter(ld | 0x00, 0, 0, 4),  # A = arch
        _SockFilter(jeq | 0x05, 1, 0, arch),
        _SockFilter(ret, 0, 0, kill_arch),
        _SockFilter(ld | 0x00, 0, 0, 0),  # A = nr
        _SockFilter(jeq | 0x05, 0, 6, socket_nr),
        _SockFilter(ld | 0x00, 0, 0, 16),  # A = args[0], the domain
        _SockFilter(jeq | 0x05, 3, 0, af_inet),
        _SockFilter(jeq | 0x05, 2, 0, af_inet6),
        _SockFilter(jeq | 0x05, 1, 0, af_packet),
        _SockFilter(jeq | 0x05, 0, 1, af_netlink),
        _SockFilter(ret, 0, 0, errno_eafnosupport),
        _SockFilter(ret, 0, 0, allow),
    ]
    # `jmp` is unused above; every branch either falls through or returns.
    del jmp

    buffer = (_SockFilter * len(program))(*program)
    fprog = _SockFprog(len=len(program), filter=buffer)

    libc = _libc()
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return False, "PR_SET_NO_NEW_PRIVS was refused"
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        return False, f"PR_SET_SECCOMP was refused (errno {ctypes.get_errno()})"
    return True, "seccomp denies AF_INET/AF_INET6"


# --------------------------------------------------------------------------- #
# Windows: lower this process's own integrity level
# --------------------------------------------------------------------------- #
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25
SE_GROUP_INTEGRITY = 0x00000020
LOW_INTEGRITY_SID = "S-1-16-4096"


def _lower_integrity() -> tuple[bool, str]:
    """Drops this process to Low integrity.

    Done by the child to itself rather than by the parent at spawn. Windows
    permits a process to *lower* its own integrity level (raising is refused),
    and the alternative -- a duplicated token handed to ``CreateProcessAsUserW``
    -- would mean replacing ``subprocess.Popen`` with a hand-rolled process
    object reimplementing ``poll``, ``wait``, ``terminate`` and the stdout pipe.
    That is a large amount of surface to own for the same end state.

    Reads keep working: the default mandatory policy is no-write-**up**, so the
    interpreter and site-packages stay readable. Writes outside the workspace,
    which is labelled Low by the parent, are refused by the kernel.
    """
    if sys.platform != "win32":
        return False, "not applicable"

    try:
        import ctypes.wintypes as wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

        class SidAndAttributes(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class TokenMandatoryLabel(ctypes.Structure):
            _fields_ = [("Label", SidAndAttributes)]

        # GetCurrentProcess returns the pseudo-handle -1, which as an unsigned
        # 64-bit value overflows the 32-bit int ctypes would otherwise guess
        # for an unannotated argument -- every call taking a HANDLE needs its
        # argtypes/restype declared explicitly, or marshalling silently breaks
        # on 64-bit Windows.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
        advapi32.GetLengthSid.restype = wintypes.DWORD
        advapi32.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        advapi32.SetTokenInformation.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        sid = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)):
            return False, f"could not build the Low integrity SID (error {ctypes.get_last_error()})"

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_ADJUST_DEFAULT | TOKEN_QUERY, ctypes.byref(token)
        ):
            return False, f"could not open this process's token (error {ctypes.get_last_error()})"

        try:
            label = TokenMandatoryLabel()
            label.Label.Sid = sid
            label.Label.Attributes = SE_GROUP_INTEGRITY
            size = ctypes.sizeof(label) + advapi32.GetLengthSid(sid)
            if not advapi32.SetTokenInformation(token, TokenIntegrityLevel, ctypes.byref(label), size):
                return False, f"SetTokenInformation was refused (error {ctypes.get_last_error()})"
        finally:
            kernel32.CloseHandle(token)
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError) as exc:
        return False, f"integrity level could not be lowered ({exc})"

    return True, "running at Low integrity"


# --------------------------------------------------------------------------- #
# POSIX resource limits
# --------------------------------------------------------------------------- #
def _apply_memory_limit(mem_bytes: int) -> tuple[bool, str]:
    if mem_bytes <= 0:
        return False, "no ceiling configured"
    try:
        import resource
    except ImportError:
        # Windows bounds the child through a job object, applied by the parent
        # before the child exists -- there is nothing to do here.
        return False, "enforced by the parent's job object"
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = mem_bytes if hard in (resource.RLIM_INFINITY, -1) else min(mem_bytes, hard)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
    except (ValueError, OSError) as exc:
        return False, f"RLIMIT_AS was refused ({exc})"
    return True, f"RLIMIT_AS {mem_bytes} bytes"


def _apply_process_limit(max_processes: int) -> tuple[bool, str]:
    if max_processes <= 0:
        return False, "no ceiling configured"
    try:
        import resource
    except ImportError:
        return False, "enforced by the parent's job object"
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        ceiling = max_processes if hard in (resource.RLIM_INFINITY, -1) else min(max_processes, hard)
        resource.setrlimit(resource.RLIMIT_NPROC, (ceiling, hard))
    except (ValueError, OSError, AttributeError) as exc:
        return False, f"RLIMIT_NPROC was refused ({exc})"
    return True, f"RLIMIT_NPROC {max_processes}"


# --------------------------------------------------------------------------- #
# The two phases
# --------------------------------------------------------------------------- #
def _feature(enforced: bool, detail: str) -> dict:
    return {"enforced": enforced, "detail": detail}


def apply_policy(policy: dict) -> dict:
    """Applies everything that must precede the daemon. Returns what stuck.

    macOS and Windows are absent by design: both are applied by the parent at
    spawn -- `sandbox-exec` wraps the command line, and a job object and token
    have to exist before the process does.
    """
    report: dict[str, dict] = {}

    cache_dir = policy.get("cache_dir")
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as exc:
            # Best-effort only, and not part of the report -- a missing cache
            # directory means a slower first import, not an unenforced
            # restriction. stdlib logging only: this module imports nothing
            # from `src`.
            logging.getLogger(__name__).debug("Could not create the sandbox cache directory: %s", exc)

    if sys.platform.startswith("linux"):
        report["filesystem"] = _feature(*_apply_landlock(policy))
    elif sys.platform == "darwin":
        report["filesystem"] = _feature(True, "sandbox-exec profile applied by the parent")
    elif sys.platform == "win32":
        if policy.get("windows_lower_integrity", True):
            report["filesystem"] = _feature(*_lower_integrity())
        else:
            # The parent could not label the workspace Low (commonly: the
            # session lives under a directory this account does not own the
            # ACL of). Lowering integrity anyway would leave the child unable
            # to write even its own pid file, so the boundary is left
            # unenforced here rather than making the runtime unusable.
            report["filesystem"] = _feature(
                False, "workspace could not be labeled Low; integrity left unchanged so writes still work"
            )

    report["memory"] = _feature(*_apply_memory_limit(int(policy.get("mem_bytes") or 0)))
    report["processes"] = _feature(*_apply_process_limit(int(policy.get("max_processes") or 0)))
    return report


def seal_network(policy: dict) -> dict:
    """Applies the network policy. Called after the daemon is listening."""
    if (policy.get("network") or "deny") != "deny":
        return {"network": _feature(False, "outbound network is allowed by configuration")}

    if sys.platform.startswith("linux"):
        return {"network": _feature(*_seccomp_deny_inet())}
    if sys.platform == "darwin":
        return {"network": _feature(True, "sandbox-exec denies all but loopback")}
    return {
        "network": _feature(
            False,
            "not enforced on Windows: blocking outbound traffic needs WFP (administrator) "
            "or an AppContainer, which would require re-ACLing this Python installation",
        )
    }


__all__ = ["apply_policy", "landlock_abi", "seal_network"]
