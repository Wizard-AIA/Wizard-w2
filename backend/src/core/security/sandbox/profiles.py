"""Platform artifacts generated from a policy.

Pure functions with no side effects, so what the sandbox will be told can be
asserted exactly in a test on any OS -- which is the only way the macOS profile
gets reviewed at all from a Windows- or Linux-developer machine.
"""

from __future__ import annotations

from src.core.security.sandbox.policy import SandboxPolicy


def _sbpl_string(value: str) -> str:
    """Quotes a path for SBPL, which is a Scheme dialect."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sbpl_profile(policy: SandboxPolicy) -> str:
    """A ``sandbox-exec`` profile denying everything the policy does not grant.

    Written deny-by-default. An allowlist that starts from ``(allow default)``
    and subtracts is the shape that quietly stops containing anything the day a
    new capability is added to the OS.

    Loopback is granted explicitly because the daemon protocol is a loopback
    socket; that is the one hole, and it is stated rather than implied.
    """
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow sysctl-read)",
        "(allow sysctl*)",
        "(allow mach-lookup)",
        "(allow signal (target self))",
        "(allow file-read-metadata)",
        "(allow user-preference-read)",
        "(allow ipc-posix-shm)",
    ]

    for root in policy.readable:
        lines.append(f"(allow file-read* (subpath {_sbpl_string(root)}))")
    for root in policy.writable:
        lines.append(f"(allow file-read* file-write* (subpath {_sbpl_string(root)}))")

    # Special devices and temp directories needed by Python's dyld/ctypes/locale runtime.
    # macOS symlinks (/var -> /private/var, /tmp -> /private/tmp, /etc -> /private/etc) require
    # both paths allowed in SBPL.
    lines.append(
        '(allow file-read* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/urandom") (literal "/dev/random") (literal "/dev/dtracehelper"))'
    )
    lines.append('(allow file-write-data (literal "/dev/null") (literal "/dev/zero"))')
    lines.append(
        '(allow file-read* (subpath "/private/var") (subpath "/var") (subpath "/private/tmp") (subpath "/tmp") (subpath "/private/etc") (subpath "/etc"))'
    )
    lines.append(
        '(allow file-read* file-write* (subpath "/private/tmp") (subpath "/tmp") (subpath "/private/var/tmp") (subpath "/var/tmp"))'
    )

    if policy.network == "deny":
        lines.append("(deny network*)")
        lines.append('(allow network-bind (local ip "localhost:*"))')
        lines.append('(allow network-outbound (remote ip "localhost:*"))')
    else:
        lines.append("(allow network*)")

    return "\n".join(lines) + "\n"


__all__ = ["sbpl_profile"]
