from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from agent_relay.presets import (
    CODEX_READ_PERMISSION_ARGUMENTS,
    CODEX_WRITE_PERMISSION_ARGUMENTS,
)


PROBE_SCRIPT = """
import pathlib
import sys

inside = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
try:
    outside = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
except OSError:
    outside = "DENIED"
print("INSIDE=" + inside)
print("OUTSIDE=" + outside)
"""


class CodexNativeContainmentTestCase(unittest.TestCase):
    def test_permission_profiles_deny_reads_outside_workspace(self) -> None:
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("Codex CLI is not installed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            inside_token = "inside-" + uuid.uuid4().hex
            outside_token = "outside-" + uuid.uuid4().hex
            inside_path = workspace / "inside.txt"
            outside_path = outside / "outside.txt"
            inside_path.write_text(inside_token, encoding="utf-8")
            outside_path.write_text(outside_token, encoding="utf-8")

            for profile_name, profile_arguments in (
                ("agent-relay-read", CODEX_READ_PERMISSION_ARGUMENTS),
                ("agent-relay-write", CODEX_WRITE_PERMISSION_ARGUMENTS),
            ):
                with self.subTest(profile=profile_name):
                    completed = subprocess.run(
                        [
                            codex,
                            "sandbox",
                            *profile_arguments,
                            "--permission-profile",
                            profile_name,
                            "-C",
                            str(workspace),
                            sys.executable,
                            "-c",
                            PROBE_SCRIPT,
                            str(inside_path),
                            str(outside_path),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    combined = completed.stdout + completed.stderr
                    if "sandbox_apply: Operation not permitted" in combined:
                        self.skipTest(
                            "the current host does not permit a nested Codex sandbox"
                        )
                    self.assertEqual(completed.returncode, 0, combined)
                    self.assertIn("INSIDE=" + inside_token, completed.stdout)
                    self.assertIn("OUTSIDE=DENIED", completed.stdout)
                    self.assertNotIn(outside_token, combined)


if __name__ == "__main__":
    unittest.main()
