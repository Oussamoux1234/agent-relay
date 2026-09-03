"""Cross-platform provider process-tree lifecycle management."""

from __future__ import annotations

import ctypes
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .errors import ValidationError


WINDOWS_BATCH_SUFFIXES = frozenset((".bat", ".cmd"))


def _resolve_windows_executable(
    command: str,
    working_directory: Path,
    environment: Dict[str, str],
) -> str:
    candidate = Path(command)
    has_directory = candidate.parent != Path(".")
    if has_directory:
        if not candidate.is_absolute():
            candidate = working_directory / candidate
        resolved = candidate.resolve()
        executable = str(resolved) if resolved.is_file() else None
    else:
        executable = shutil.which(command, path=environment.get("PATH"))
    if executable is None:
        raise FileNotFoundError("configured Windows executable was not found")
    if Path(executable).suffix.lower() in WINDOWS_BATCH_SUFFIXES:
        raise ValidationError(
            "Windows .bat and .cmd provider shims are not accepted because they require "
            "shell parsing; configure the underlying .exe command instead"
        )
    return executable


def validate_executable(
    argv: Sequence[str],
    working_directory: Path,
    environment: Dict[str, str],
) -> None:
    if os.name == "nt":
        try:
            _resolve_windows_executable(argv[0], working_directory, environment)
        except FileNotFoundError:
            # Preserve the adapter contract: a missing command becomes a clean,
            # persisted launch failure rather than a preflight validation error.
            pass


class _WindowsJob:
    def __init__(self) -> None:
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        self._kernel32 = kernel32
        self._handle = handle
        self._handle_type = wintypes.HANDLE

    def assign(self, process: subprocess.Popen) -> None:
        process_handle = self._handle_type(int(getattr(process, "_handle")))
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _windows_spawn(
    argv: Sequence[str],
    working_directory: Path,
    environment: Dict[str, str],
    stdout: Any,
    stderr: Any,
) -> subprocess.Popen:
    resolved_argv = list(argv)
    resolved_argv[0] = _resolve_windows_executable(
        resolved_argv[0],
        working_directory,
        environment,
    )
    launcher = Path(__file__).with_name("_windows_launcher.py").resolve()
    job = _WindowsJob()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, str(launcher)] + resolved_argv,
            cwd=str(working_directory),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            shell=False,
        )
        job.assign(process)
        if process.stdin is None:
            raise OSError("Windows provider startup gate has no input pipe")
        process.stdin.write(b"\0")
        process.stdin.flush()
        setattr(process, "_agent_relay_windows_job", job)
        return process
    except BaseException:
        if process is not None:
            try:
                job.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        job.close()
        raise


def spawn_process(
    argv: Sequence[str],
    working_directory: Path,
    environment: Dict[str, str],
    stdin: Any,
    stdout: Any,
    stderr: Any,
) -> subprocess.Popen:
    if os.name == "nt":
        return _windows_spawn(argv, working_directory, environment, stdout, stderr)
    return subprocess.Popen(
        list(argv),
        cwd=str(working_directory),
        env=environment,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        shell=False,
    )


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _windows_job(process: subprocess.Popen) -> Optional[_WindowsJob]:
    job = getattr(process, "_agent_relay_windows_job", None)
    return job if isinstance(job, _WindowsJob) else None


def release_process_tree(process: subprocess.Popen) -> None:
    if os.name != "nt":
        return
    job = _windows_job(process)
    if job is not None:
        job.close()
        setattr(process, "_agent_relay_windows_job", None)


def terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        job = _windows_job(process)
        try:
            if job is not None:
                job.terminate()
            else:
                process.terminate()
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass
        finally:
            release_process_tree(process)
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    except (ChildProcessError, OSError):
        pass

    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        except (ChildProcessError, OSError):
            pass

        deadline = time.monotonic() + 2
        while _process_group_exists(process.pid) and time.monotonic() < deadline:
            time.sleep(0.01)
