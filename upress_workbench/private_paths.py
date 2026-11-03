"""Path policy for handling private manuscript material on a local machine."""

from __future__ import annotations

import os
from pathlib import Path

from . import config
from .security import SafetyError, _is_reparse_point


def private_path(value: str | Path) -> Path:
    """Check existing paths before reading inputs or creating private output."""
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise SafetyError("Select an absolute local path without parent traversal.")
    if os.name == "nt":
        if path.drive.startswith("\\\\") or any(":" in part for part in path.parts[1:]):
            raise SafetyError("Network paths, device namespaces and alternate data streams are unsupported.")
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 4:
            raise SafetyError("Select a local drive for private review.")
    for part in (*reversed(path.parents), path):
        if _is_reparse_point(part):
            raise SafetyError("Private review paths cannot traverse links or reparse points.")
    path = path.resolve(strict=True)
    for folder in (path, *path.parents):
        if folder == config.APP_ROOT or os.path.lexists(folder / ".git") or (
            (folder / "HEAD").is_file() and (folder / "objects").is_dir() and (folder / "refs").is_dir()
        ):
            raise SafetyError("Keep private sources and outputs outside the application and Git repositories.")
    for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        sync_root = os.getenv(key)
        if sync_root and path.is_relative_to(Path(sync_root).resolve()):
            raise SafetyError("Select a private local folder outside OneDrive.")
    return path
