"""What this machine can actually enforce, and why not where it cannot.

The milestone's requirement is verifiable containment rather than documented
containment, and the first half of that is refusing to claim anything that was
not applied. Every answer here carries a reason, so `/settings` can say "not
enforced, because ..." instead of rendering a silent gap.

Cheap and never raises: this is consulted on a page load and at every spawn.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache


#: The features a policy can ask for, in the order the UI shows them.
FEATURES = ("filesystem", "network", "memory", "processes")


@dataclass(frozen=True)
class Feature:
    key: str
    supported: bool
    detail: str

    def to_dict(self) -> dict:
        return {"key": self.key, "supported": self.supported, "detail": self.detail}


@dataclass(frozen=True)
class SandboxCapability:
    platform: str
    mechanism: str
    features: tuple[Feature, ...]

    @property
    def any_supported(self) -> bool:
        return any(feature.supported for feature in self.features)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "mechanism": self.mechanism,
            "features": [feature.to_dict() for feature in self.features],
        }


def _probe_sandbox_exec() -> tuple[bool, str]:
    """Runs the smallest possible profile to see whether `sandbox-exec` works.

    Probed rather than inferred from the OS version. `sandbox-exec` has carried
    a deprecation warning since 10.14 and is still present and functional well
    past it, so a version check would refuse a mechanism that works; and the
    documented replacement, App Sandbox entitlements, needs a signed application
    bundle, which a `git clone` does not have. Asking the binary is the only
    answer that stays true.
    """
    binary = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
    if not os.path.exists(binary):
        return False, "sandbox-exec is not present on this system"
    # Probe with a deny-default profile: on modern macOS (14+ Sonoma / 15+ Sequoia),
    # sandboxd terminates non-entitled custom deny-default processes with SIGABRT (-6).
    profile = "(version 1)(deny default)(allow process-exec)(allow process-fork)(allow sysctl-read)(allow file-read*)"
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            [binary, "-p", profile, sys.executable, "-c", "import sys"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"sandbox-exec could not be run ({exc})"
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        return (
            False,
            f"sandbox-exec cannot enforce profiles on this macOS kernel ({detail or 'exit code ' + str(result.returncode)})",
        )
    return True, "sandbox-exec accepts a profile"


def _linux_capability() -> SandboxCapability:
    from src.core.security.sandbox.child import landlock_abi

    abi = landlock_abi()
    filesystem = (
        Feature("filesystem", True, f"Landlock ABI {abi}")
        if abi > 0
        else Feature("filesystem", False, "this kernel has no Landlock (5.13+ required)")
    )
    return SandboxCapability(
        platform="linux",
        mechanism="landlock+seccomp",
        features=(
            filesystem,
            Feature("network", True, "seccomp refuses AF_INET and AF_INET6 sockets"),
            Feature("memory", True, "RLIMIT_AS"),
            Feature("processes", True, "RLIMIT_NPROC"),
        ),
    )


def _macos_capability() -> SandboxCapability:
    supported, detail = _probe_sandbox_exec()
    return SandboxCapability(
        platform="darwin",
        mechanism="sandbox-exec",
        features=(
            Feature("filesystem", supported, detail),
            Feature("network", supported, detail if not supported else "the profile denies all but loopback"),
            Feature("memory", True, "RLIMIT_AS"),
            Feature("processes", True, "RLIMIT_NPROC"),
        ),
    )


def _windows_capability() -> SandboxCapability:
    return SandboxCapability(
        platform="win32",
        mechanism="job-object+low-integrity",
        features=(
            Feature("filesystem", True, "a low integrity level token blocks writes outside the workspace"),
            Feature(
                "network",
                False,
                "not enforced on Windows: blocking outbound traffic needs WFP, which requires "
                "administrator, or an AppContainer, which would require re-ACLing this Python "
                "installation for the container identity",
            ),
            Feature("memory", True, "job object ProcessMemoryLimit"),
            Feature("processes", True, "job object ActiveProcessLimit"),
        ),
    )


@lru_cache(maxsize=1)
def detect() -> SandboxCapability:
    """What this OS offers. Measured once; the kernel does not change under us."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_capability()
        if sys.platform == "darwin":
            return _macos_capability()
        if sys.platform == "win32":
            return _windows_capability()
    except Exception as exc:  # noqa: BLE001 - a diagnostics path must not fail
        return SandboxCapability(
            platform=sys.platform,
            mechanism="none",
            features=tuple(Feature(name, False, f"detection failed ({exc})") for name in FEATURES),
        )
    return SandboxCapability(
        platform=sys.platform,
        mechanism="none",
        features=tuple(Feature(name, False, f"no sandbox is implemented for {sys.platform}") for name in FEATURES),
    )


__all__ = ["FEATURES", "Feature", "SandboxCapability", "detect"]
