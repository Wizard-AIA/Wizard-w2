"""Docker security validation."""


class SecurityPolicyError(Exception):
    """Raised when a container configuration violates security policies."""

    pass


def validate_container_config(config: dict) -> None:
    """Validate a Docker container configuration against security policies.

    Args:
        config: The Docker API container creation configuration dict.

    Raises:
        SecurityPolicyError: If the configuration violates any security rules.
    """
    host_config = config.get("HostConfig") if isinstance(config.get("HostConfig"), dict) else config

    if host_config.get("Privileged"):
        raise SecurityPolicyError("Container cannot run in privileged mode.")

    binds = host_config.get("Binds") or []
    for bind in binds:
        # Bind format: host_path:container_path[:mode]
        parts = bind.split(":")
        host_path = parts[0]
        forbidden_paths = ["/", "/etc", "/root", "/var/run/docker.sock"]
        for forbidden in forbidden_paths:
            if host_path == forbidden or host_path.startswith(forbidden + "/"):
                raise SecurityPolicyError(f"Mounting {forbidden} is not allowed.")

    cap_drop = host_config.get("CapDrop") or []
    readonly_rootfs = host_config.get("ReadonlyRootfs", False)

    if not readonly_rootfs and "ALL" not in cap_drop:
        raise SecurityPolicyError("Container must have ReadonlyRootfs=True or CapDrop=['ALL'].")

    user = config.get("User", "")
    if not user or user == "0" or user == "root":
        raise SecurityPolicyError("Container must run as a non-root user.")
