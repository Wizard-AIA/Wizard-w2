"""Pins for security-audit findings M-10 (#75) and M-12 (#77).

M-10: `_restrict_windows` silently kept inherited permissions whenever the
`icacls` utility itself was unavailable (Server Core, Nano Server, AppLocker).
M-12: `DataPolicy.per_dataset` was keyed by raw filename, so a case variant of
an already-policed dataset silently fell back to the session default instead
of the override meant for it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.data_mode import DataPolicy


# --------------------------------------------------------------------------- #
# M-12 -- per-dataset policy keys are normalized
# --------------------------------------------------------------------------- #
def test_a_case_variant_of_a_policed_dataset_still_finds_its_override() -> None:
    policy = DataPolicy(schema_only=False)
    policy.set_for("Dataset.CSV", True)
    assert policy.schema_only_for("dataset.csv")
    assert policy.schema_only_for("  DATASET.CSV  ")


def test_a_case_variant_origin_still_finds_its_override() -> None:
    policy = DataPolicy(schema_only=False)
    policy.set_for("Sales", True)
    assert policy.schema_only_for(dataset=None, origin="sales")


def test_clearing_a_case_variant_drops_the_original_override() -> None:
    policy = DataPolicy(schema_only=False)
    policy.set_for("Dataset.CSV", True)
    assert policy.clear_for("dataset.csv")
    assert not policy.schema_only_for("Dataset.CSV")


def test_forgetting_a_case_variant_drops_the_original_override() -> None:
    policy = DataPolicy(schema_only=False)
    policy.set_for("Dataset.CSV", True)
    policy.forget("dataset.csv")
    assert not policy.schema_only_for("Dataset.CSV")


def test_rekey_moves_an_override_to_a_new_name_case_insensitively() -> None:
    """Exercises the connection-rename path in `routes/connections.py`, which
    used to touch `per_dataset` directly and would have re-broken this."""
    policy = DataPolicy(schema_only=False)
    policy.set_for("Old Name", True)
    policy.rekey("old name", "New Name")
    assert policy.schema_only_for("new name")
    assert not policy.schema_only_for("old name")


# --------------------------------------------------------------------------- #
# M-10 -- Windows ACL restriction falls back when icacls is unavailable
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "win32", reason="exercises Win32 ACL APIs")
def test_restriction_falls_back_to_native_apis_when_icacls_is_unavailable(tmp_path: Path) -> None:
    from src.utils import fileperms

    target = tmp_path / "connections.json"
    target.write_text("{}", encoding="utf-8")

    with patch.object(fileperms, "_icacls", return_value=False) as icacls:
        fileperms._restrict_windows(target, "test file")

    icacls.assert_called_once()
    # Still writable by the running account after the native fallback ran.
    with target.open("a"):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="exercises Win32 ACL APIs")
def test_restriction_warns_but_does_not_raise_when_every_path_is_unavailable(tmp_path: Path) -> None:
    from src.utils import fileperms

    target = tmp_path / "connections.json"
    target.write_text("{}", encoding="utf-8")

    with (
        patch.object(fileperms, "_icacls", return_value=False),
        patch.object(fileperms, "_set_entry_native", return_value=False),
    ):
        fileperms._restrict_windows(target, "test file")  # must not raise

    with target.open("a"):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="exercises Win32 ACL APIs")
def test_a_native_restriction_that_locks_out_the_owner_is_rolled_back(tmp_path: Path) -> None:
    from src.utils import fileperms

    target = tmp_path / "connections.json"
    target.write_text("{}", encoding="utf-8")

    with (
        patch.object(fileperms, "_icacls", return_value=False),
        patch.object(fileperms, "_set_entry_native", return_value=True),
        patch.object(fileperms, "_is_writable", return_value=False),
        patch.object(fileperms, "_reset_native") as reset_native,
    ):
        fileperms._restrict_windows(target, "test file")

    reset_native.assert_called_once()
