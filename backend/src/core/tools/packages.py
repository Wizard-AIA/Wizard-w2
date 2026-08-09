"""Installing a library the analysis needs, from the parent process.

This used to happen inside the daemon, reactively, on ``ModuleNotFoundError`` --
which put it *after* the program had started running and behind whatever network
policy the runtime was under. Both are problems now:

* a sandboxed child is denied outbound network, so ``pip`` inside it cannot
  reach an index at all;
* Milestone 2's ``library_install`` gate already fires **before** execution, so
  consent was being asked in one place and acted on in another.

Doing it here puts the decision and the action together, and keeps the install
out of the environment the backend itself runs in: everything lands in
``<workspace>/.libs``, which the daemon has on ``sys.path`` and which is thrown
away with the session.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from src.utils.logging import logger


#: A distribution name pip will accept as a plain package specifier -- not a
#: flag, a VCS URL, a `pkg @ url` direct reference, or a filesystem path.
#: `pip install` positional arguments go straight to its own argument parser
#: regardless of the fact that ``subprocess.run`` is invoked without a shell,
#: so a value starting with ``-`` (e.g. ``--index-url=http://evil``) or
#: containing ``@``/``://`` (a direct reference pip will fetch and build) is
#: rejected outright rather than escaped -- there is no argv position that
#: makes those safe to pass through. Mirrors PyPI's own package-name grammar
#: (PEP 508): letters, digits, ``.``, ``_``, ``-``, bounded in length.
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,213}[A-Za-z0-9])?$")


#: Import name -> distribution name, for the cases where they differ. A missing
#: entry is not a problem: the import name is tried as-is, which is right for the
#: large majority of packages. Rendered into the daemon too, so the container
#: path and this one cannot disagree about what `sklearn` is called.
DISTRIBUTION_NAMES: dict[str, str] = {
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "PIL": "pillow",
    "docx": "python-docx",
    "cv2": "opencv-python-headless",
    "dateutil": "python-dateutil",
    "sqlalchemy": "SQLAlchemy",
    "skimage": "scikit-image",
    "statsmodels.api": "statsmodels",
    "mpl_toolkits": "matplotlib",
    "pyarrow.parquet": "pyarrow",
    "Levenshtein": "python-Levenshtein",
    "umap": "umap-learn",
    # The CPU build. The default xgboost wheel bundles CUDA and is 154 MB
    # compressed for a machine that in this context has no GPU anyway.
    "xgboost": "xgboost-cpu",
}

#: Where a session's on-demand libraries live, relative to its workspace.
LIBS_DIRNAME = ".libs"


def libs_dir(workspace: Path) -> Path:
    return Path(workspace) / LIBS_DIRNAME


def distribution_for(module: str) -> str:
    return DISTRIBUTION_NAMES.get(module, module)


def is_allowed_package(name: str) -> bool:
    """Whether ``name`` is safe to hand to pip as a bare package argument."""
    return bool(_PACKAGE_NAME_RE.match(name))


def install(workspace: Path, modules: frozenset[str] | set[str], timeout: int = 300) -> tuple[bool, str]:
    """Installs ``modules`` into the session's own library directory.

    ``--target`` rather than a virtualenv: the child is already this
    interpreter, so a directory on its ``sys.path`` is the whole mechanism, and
    there is no second interpreter to keep in step.

    Returns ``(ok, detail)``. A failure is reported rather than raised -- the
    caller records a failed step and the loop routes around it, exactly as it
    does for any other sub-task that did not work.
    """
    if not modules:
        return True, "nothing to install"

    from src.config import settings

    # The master switch for on-demand installs, honoured on both backends. The
    # test suite pins it off: a consent gate that a test approves would
    # otherwise reach a real index, which is the one thing the suite may never
    # do -- and it did, exactly once, before this check existed.
    if not settings.SANDBOX_ALLOW_RUNTIME_PIP:
        return False, "on-demand installs are disabled (SANDBOX_ALLOW_RUNTIME_PIP)"

    target = libs_dir(workspace)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"could not create {target}: {exc}"

    packages = sorted({distribution_for(name) for name in modules})
    rejected = [name for name in packages if not is_allowed_package(name)]
    if rejected:
        return False, f"refusing to install {', '.join(rejected)}: not a valid package name"

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--quiet",
        "--disable-pip-version-check",
        "--target",
        str(target),
        *packages,
    ]

    logger.info("Installing libraries for a session", packages=packages, target=str(target))
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    except subprocess.TimeoutExpired:
        return False, f"pip timed out after {timeout}s installing {', '.join(packages)}"
    except OSError as exc:
        return False, f"could not run pip: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        # pip's own last line is the useful one; the rest is resolver noise.
        tail = detail.splitlines()[-1] if detail else "no output"
        return False, f"pip failed installing {', '.join(packages)}: {tail}"

    return True, f"installed {', '.join(packages)}"


__all__ = ["DISTRIBUTION_NAMES", "LIBS_DIRNAME", "distribution_for", "install", "is_allowed_package", "libs_dir"]
