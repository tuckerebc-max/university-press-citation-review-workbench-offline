"""Path policy for handling private manuscript material on a local machine."""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config
from .security import SafetyError, _is_reparse_point


def require_private_directory(path: Path) -> None:
    """Verify the newly created stage before writing any manuscript bytes."""
    if os.name != "nt":
        if path.stat().st_mode & 0o077:
            raise SafetyError("The staging filesystem did not enforce private directory permissions.")
        return
    import ctypes
    from ctypes import wintypes

    # 0700 handling varies across Windows Python versions and filesystems.
    # Inspect the resulting DACL rather than relying on the requested mode.
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    size = wintypes.DWORD()
    dacl_information = 4
    security.GetFileSecurityW(str(path), dacl_information, None, 0, ctypes.byref(size))
    if not size.value:
        raise SafetyError("Cannot verify private Windows directory permissions.")
    descriptor = ctypes.create_string_buffer(size.value)
    if not security.GetFileSecurityW(str(path), dacl_information, descriptor, size, ctypes.byref(size)):
        raise SafetyError("Cannot read the staging directory permissions.")
    sddl = wintypes.LPWSTR()
    if not security.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, dacl_information, ctypes.byref(sddl), None):
        raise SafetyError("Cannot verify the staging directory access list.")
    try:
        policy = sddl.value or ""
        entries = re.findall(r"\(([^()]*)\)", policy)
        if not policy.startswith("D:P") or not entries or any(e.split(";")[-1] not in {"OW", "SY", "BA"} for e in entries):
            raise SafetyError("Use a local filesystem and a current Python runtime that enforce owner-only private folders.")
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(sddl, ctypes.c_void_p))


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
