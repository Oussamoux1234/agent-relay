from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_relay.adapters import CliAgentAdapter
from agent_relay.errors import ValidationError
from agent_relay.models import AgentSpec, TaskCheckpoint
from agent_relay.process_control import spawn_process as real_spawn_process
from agent_relay.storage import RelayStore


PROVIDER_TREE_SCRIPT = r"""
import pathlib
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="ascii")
time.sleep(60)
"""


@unittest.skipUnless(os.name == "nt", "native Windows tests")
class WindowsStateSecurityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_junction(self, link: Path, target: Path) -> None:
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("this Windows host cannot create a junction")

    def test_rejects_tasks_junction_before_initialization(self) -> None:
        state = self.root / "state"
        outside = self.root / "outside"
        state.mkdir()
        outside.mkdir()
        self.make_junction(state / "tasks", outside)

        with self.assertRaisesRegex(ValidationError, "junction|reparse"):
            RelayStore(state)

        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_tasks_junction_substitution_after_initialization(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        store = RelayStore(self.root / "state")
        store.tasks_dir.rmdir()
        self.make_junction(store.tasks_dir, outside)

        with self.assertRaisesRegex(ValidationError, "junction|reparse"):
            store.create_task(TaskCheckpoint.create("Blocked", "Reject junction"))

        self.assertEqual(list(outside.iterdir()), [])


@unittest.skipUnless(os.name == "nt", "native Windows tests")
class WindowsProcessTreeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.adapter = CliAgentAdapter()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def process_is_running(process_id: int) -> bool:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, process_id)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)

    def wait_for_pid(self, path: Path) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                return int(path.read_text(encoding="ascii"))
            except (FileNotFoundError, ValueError):
                time.sleep(0.02)
        self.fail("provider descendant pid was not recorded")

    def assert_process_stopped(self, process_id: int) -> None:
        deadline = time.monotonic() + 5
        while self.process_is_running(process_id) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.process_is_running(process_id))

    def tree_spec(self, pid_path: Path, timeout_seconds: int = 1) -> AgentSpec:
        return AgentSpec(
            agent_id="windows-tree",
            display_name="Windows tree",
            command=(sys.executable, "-c", PROVIDER_TREE_SCRIPT, str(pid_path)),
            timeout_seconds=timeout_seconds,
        )

    def test_timeout_terminates_provider_descendants(self) -> None:
        pid_path = self.root / "timeout-child.pid"

        result = self.adapter.execute(self.tree_spec(pid_path), "wait", self.root)

        child_pid = self.wait_for_pid(pid_path)
        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.timed_out)
        self.assert_process_stopped(child_pid)

    def test_keyboard_interrupt_and_system_exit_terminate_provider_descendants(self) -> None:
        for index, exception_type in enumerate((KeyboardInterrupt, SystemExit)):
            with self.subTest(exception_type=exception_type.__name__):
                pid_path = self.root / ("interrupt-child-%d.pid" % index)
                started = []

                def spawn(*args, **kwargs):
                    process = real_spawn_process(*args, **kwargs)
                    started.append(process)

                    def interrupt(*_args, **_kwargs):
                        self.wait_for_pid(pid_path)
                        raise exception_type

                    process.communicate = interrupt
                    return process

                with mock.patch("agent_relay.adapters.spawn_process", side_effect=spawn):
                    with self.assertRaises(exception_type):
                        self.adapter.execute(
                            self.tree_spec(pid_path, timeout_seconds=10),
                            "interrupt",
                            self.root,
                        )

                child_pid = self.wait_for_pid(pid_path)
                self.assertEqual(len(started), 1)
                self.assertIsNotNone(started[0].poll())
                self.assert_process_stopped(child_pid)

    def test_argument_transport_preserves_shell_metacharacters(self) -> None:
        prompt = "literal & whoami | echo %PATH% > should-not-exist"
        spec = AgentSpec(
            agent_id="argument-agent",
            display_name="Argument agent",
            command=(sys.executable, "-c", "import sys; print(sys.argv[1])"),
            prompt_transport="argument",
        )

        result = self.adapter.execute(spec, prompt, self.root)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.stdout.strip(), prompt)
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_batch_provider_shims_fail_before_launch(self) -> None:
        shim = self.root / "provider.cmd"
        shim.write_text("@echo launched>launched.txt\r\n", encoding="utf-8")
        spec = AgentSpec(
            agent_id="batch-agent",
            display_name="Batch agent",
            command=(str(shim),),
        )

        with self.assertRaisesRegex(ValidationError, "shell parsing"):
            self.adapter.execute(spec, "do not launch", self.root)

        self.assertFalse((self.root / "launched.txt").exists())


if __name__ == "__main__":
    unittest.main()
