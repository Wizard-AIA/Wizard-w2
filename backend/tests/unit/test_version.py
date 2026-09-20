"""Version-source regression tests for source trees and container images."""

from pathlib import Path

from src import version


def test_app_version_comes_from_the_repository_version_file() -> None:
    repository = Path(__file__).resolve().parents[3]
    assert version.APP_VERSION == (repository / "VERSION").read_text(encoding="utf-8").strip()


def test_backend_image_copies_the_version_file_beside_src() -> None:
    repository = Path(__file__).resolve().parents[3]
    dockerfile = (repository / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY VERSION /app/VERSION" in dockerfile
