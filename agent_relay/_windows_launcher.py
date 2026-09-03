"""Internal startup gate for placing native Windows providers in a Job Object."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    if os.name != "nt" or len(sys.argv) < 2:
        return 253
    if os.read(sys.stdin.fileno(), 1) != b"\0":
        return 252
    try:
        process = subprocess.Popen(
            sys.argv[1:],
            stdin=None,
            stdout=None,
            stderr=None,
            shell=False,
        )
    except (FileNotFoundError, PermissionError, OSError):
        return 251
    try:
        return process.wait()
    except BaseException:
        try:
            process.terminate()
        except OSError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())

