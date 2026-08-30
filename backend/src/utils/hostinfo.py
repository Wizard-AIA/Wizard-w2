"""What machine is this actually running on.

Every resource default in the app used to be a constant chosen for a server:
eight inference threads, a 2 GB ceiling per sandbox container, thirty-two
concurrent sessions. On a four-core laptop with a couple of gigabytes free that
is not a conservative default, it is an overcommit -- eight threads on four
cores is contention, and 32 x 2 GB is sixty-four gigabytes of containers.

``SYSTEM_PROFILE`` existed to express this and was read by nothing at all, so
the numbers were fixed whatever it said.

Detection is deliberately dependency-free: ``psutil`` would be a new install on
every deployment for three numbers that the standard library can already reach.
Everything here is total -- an unreadable value yields ``None`` and the caller
keeps its own default rather than crashing at import time, because this module
runs while ``Settings`` is being constructed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache


#: Profiles, smallest first. The boundaries are drawn where the *shape* of the
#: machine changes rather than at round numbers: below 8 GB a single 7B model
#: and a container do not comfortably coexist, and above 64 GB with 16 threads
#: you are no longer on a personal machine.
LAPTOP_MAX_RAM_GB = 24.0
HPC_MIN_RAM_GB = 64.0
HPC_MIN_CORES = 16


def _physical_cores_linux() -> int | None:
    """Distinct (socket, core) pairs from /proc/cpuinfo."""
    try:
        cores: set[tuple[str, str]] = set()
        physical_id = ""
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                key, value = key.strip(), value.strip()
                if key == "physical id":
                    physical_id = value
                elif key == "core id":
                    cores.add((physical_id, value))
        return len(cores) or None
    except OSError:
        return None


def _physical_cores_windows() -> int | None:
    """Counts RelationProcessorCore records from GetLogicalProcessorInformationEx.

    Windows exposes no /proc, and the WMI route (``Win32_Processor``) costs a
    subprocess or a COM connection -- both far too heavy for something that runs
    while ``Settings`` is being constructed.
    """
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        relation_processor_core = 0
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        length = wintypes.DWORD(0)
        kernel32.GetLogicalProcessorInformationEx(relation_processor_core, None, ctypes.byref(length))
        if not length.value:
            return None

        buffer = ctypes.create_string_buffer(length.value)
        if not kernel32.GetLogicalProcessorInformationEx(relation_processor_core, buffer, ctypes.byref(length)):
            return None

        # Each record is a variable-length struct whose first two DWORDs are
        # Relationship and Size, so the list is walked by Size rather than by a
        # fixed stride.
        count = 0
        offset = 0
        while offset + 8 <= length.value:
            size = int.from_bytes(b"".join(buffer[offset + 4 : offset + 8]), "little")
            if size <= 0:
                break
            count += 1
            offset += size
        return count or None
    except Exception:
        return None


def _physical_cores_darwin() -> int | None:
    """``hw.physicalcpu`` through libc, avoiding a `sysctl` subprocess."""
    try:
        import ctypes
        import ctypes.util

        libc_path = ctypes.util.find_library("c")
        if not libc_path:
            return None
        libc = ctypes.CDLL(libc_path)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if libc.sysctlbyname(b"hw.physicalcpu", ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            return None
        return value.value or None
    except Exception:
        return None


def _physical_cores() -> tuple[int | None, int | None]:
    """``(physical, logical)`` cores. Either may be ``None`` if unknowable.

    Local inference is memory-bandwidth bound, so hyperthreads add contention
    rather than throughput -- which makes the physical count the one worth
    having, and worth asking each platform for properly instead of halving the
    logical count and calling it detection.
    """
    logical = os.cpu_count()
    # Python 3.13+ reports the affinity-aware count; 3.11 does not.
    getter = getattr(os, "process_cpu_count", None)
    if getter is not None:
        logical = getter() or logical

    if sys.platform.startswith("linux"):
        physical = _physical_cores_linux()
    elif sys.platform == "win32":
        physical = _physical_cores_windows()
    elif sys.platform == "darwin":
        physical = _physical_cores_darwin()
    else:
        physical = None

    # A container pinned to fewer CPUs than the box has must not be told it
    # owns all the physical cores.
    if physical and logical:
        physical = min(physical, logical)
    return physical, logical


def _total_ram_bytes() -> int | None:
    """Installed RAM, or ``None`` when the platform will not say."""
    if sys.platform == "win32":
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys)
        except Exception:
            return None
        return None

    # Linux and macOS both expose these; a container sees the host's total here,
    # which is why the cgroup limit below takes precedence when present.
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, AttributeError):
        return None


def _cgroup_memory_limit_bytes() -> int | None:
    """The container's own memory ceiling, when running under one.

    Without this the backend container reads the *host's* RAM and sizes itself
    for a machine it cannot use -- the reason "detect the hardware" has to mean
    the cgroup, not the motherboard.
    """
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw in ("max", ""):
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # Unlimited cgroups report a sentinel near 2**63; treat anything absurd
        # as "no limit" rather than sizing for eight exabytes.
        if 0 < value < (1 << 46):
            return value
    return None


@dataclass(frozen=True)
class HostInfo:
    """Measured facts about the machine, plus the profile they imply."""

    #: Physical cores where the platform would say, logical otherwise.
    cores: int
    #: Hardware threads, which is what `os.cpu_count` reports.
    logical_cores: int
    ram_bytes: int | None
    containerised: bool
    #: True only when both the core count and the memory size were really read.
    detected: bool

    @property
    def ram_gb(self) -> float | None:
        return None if self.ram_bytes is None else self.ram_bytes / (1024**3)

    @property
    def profile(self) -> str:
        """``laptop`` / ``server`` / ``hpc``, inferred from cores and RAM.

        Unknown RAM resolves to ``server``: it is the middle option, and the one
        that neither starves a real server nor overcommits a laptop badly.
        """
        ram = self.ram_gb
        if ram is None:
            return "server"
        if ram <= LAPTOP_MAX_RAM_GB:
            return "laptop"
        if ram >= HPC_MIN_RAM_GB and self.cores >= HPC_MIN_CORES:
            return "hpc"
        return "server"

    def to_dict(self) -> dict[str, object]:
        return {
            "cores": self.cores,
            "logical_cores": self.logical_cores,
            "ram_gb": None if self.ram_gb is None else round(self.ram_gb, 1),
            "profile": self.profile,
            "containerised": self.containerised,
            "detected": self.detected,
        }


@lru_cache(maxsize=1)
def host_info() -> HostInfo:
    """Detected host facts, measured once per process."""
    physical, logical = _physical_cores()
    ram = _cgroup_memory_limit_bytes()
    # A cgroup limit is only readable from inside a container, so finding one is
    # itself the signal -- /.dockerenv covers unlimited containers.
    containerised = ram is not None or os.path.exists("/.dockerenv")
    if ram is None:
        ram = _total_ram_bytes()
    return HostInfo(
        cores=physical or logical or 2,
        logical_cores=logical or physical or 2,
        ram_bytes=ram,
        containerised=containerised,
        detected=physical is not None and ram is not None,
    )


__all__ = ["HostInfo", "host_info"]
