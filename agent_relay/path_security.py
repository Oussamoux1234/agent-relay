"""Small cross-platform helpers for opening security-sensitive regular files."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional


FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def open_regular_read_only(path: Path) -> Optional[int]:
    """Open one unchanged, non-reparse regular file or return None."""

    try:
        before = path.lstat()
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return None
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = None
    try:
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        if descriptor is not None:
            os.close(descriptor)
        return None
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_dev != before.st_dev
        or opened.st_ino != before.st_ino
    ):
        os.close(descriptor)
        return None
    return descriptor

