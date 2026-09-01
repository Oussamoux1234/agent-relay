from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_relay.cli import main


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_dir = str(self.root / "state")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *arguments: str):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(["--state-dir", self.state_dir, *arguments])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_cli_register_create_preview_flow(self) -> None:
        status, output, _ = self.invoke(
            "agent",
            "add",
            "codex",
            "--name",
            "Codex",
            "--command",
            sys.executable,
            "--arg=-c",
            "--arg",
            "print('ok')",
        )
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["agent"]["agent_id"], "codex")

        status, output, _ = self.invoke(
            "agent",
            "add",
            "claude-code",
            "--name",
            "Claude Code",
            "--command",
            sys.executable,
            "--arg=-c",
            "--arg",
            "print('ok')",
        )
        self.assertEqual(status, 0)

        status, output, _ = self.invoke(
            "task",
            "create",
            "--title",
            "Demo",
            "--goal",
            "Prove continuity",
            "--agent",
            "codex",
        )
        self.assertEqual(status, 0)
        task_id = json.loads(output)["task"]["task_id"]

        status, output, _ = self.invoke("handoff", task_id, "claude-code")
        self.assertEqual(status, 0)
        preview = json.loads(output)
        self.assertTrue(preview["dry_run"])
        self.assertIn("Prove continuity", preview["prompt"])

    def test_cli_returns_structured_validation_errors(self) -> None:
        status, _, error = self.invoke(
            "agent",
            "add",
            "BAD/ID",
            "--name",
            "Bad",
            "--command",
            "fake",
        )

        self.assertEqual(status, 2)
        parsed = json.loads(error)
        self.assertEqual(parsed["error_type"], "ValidationError")


if __name__ == "__main__":
    unittest.main()
