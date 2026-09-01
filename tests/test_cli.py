from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_relay.cli import main
from agent_relay.models import AgentSpec
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore


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

    def test_cli_can_set_show_and_preview_a_route(self) -> None:
        service = RelayService(RelayStore(Path(self.state_dir)))
        for agent_id, provider_id in (
            ("codex-primary", "codex-cli"),
            ("claude-backup", "claude-code"),
        ):
            service.register_agent(
                AgentSpec(
                    agent_id=agent_id,
                    display_name=agent_id,
                    command=(sys.executable, "-c", "print('ok')"),
                    capabilities=("repo-read",),
                    provider_id=provider_id,
                )
            )
        task = service.create_task(
            "Route CLI",
            "Keep the checkpoint portable",
            active_agent="codex-primary",
        )

        status, output, _ = self.invoke(
            "route",
            "set",
            task.task_id,
            "--agent",
            "codex-primary",
            "--agent",
            "claude-backup",
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output)["routing_order"],
            ["codex-primary", "claude-backup"],
        )

        status, output, _ = self.invoke("route", "show", task.task_id)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["active_agent"], "codex-primary")

        status, output, _ = self.invoke("route", "run", task.task_id)
        self.assertEqual(status, 0)
        preview = json.loads(output)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["attempts"], [])
        self.assertIn("Keep the checkpoint portable", preview["prompt"])

    def test_cli_can_preview_and_accept_a_structured_result(self) -> None:
        result_code = (
            "import json, sys; prompt = sys.stdin.read(); "
            "payload = json.loads(prompt[prompt.index('{'):]); "
            "contract = payload['handoff']['result_contract']; "
            "result = dict(contract['schema']); "
            "result['summary'] = 'Fresh CLI summary'; "
            "result['tests'] = ['cli test passed']; "
            "print(contract['begin_marker']); print(json.dumps(result)); "
            "print(contract['end_marker'])"
        )
        service = RelayService(RelayStore(Path(self.state_dir)))
        for agent_id, code in (
            ("codex-primary", result_code),
            ("codex-backup", "print('backup')"),
        ):
            service.register_agent(
                AgentSpec(
                    agent_id=agent_id,
                    display_name=agent_id,
                    command=(sys.executable, "-c", code),
                    capabilities=("repo-read",),
                    provider_id="codex-cli",
                )
            )
        task = service.create_task(
            "Result CLI",
            "Accept explicit memory",
            active_agent="codex-primary",
        )
        service.configure_route(task.task_id, ["codex-primary", "codex-backup"])
        outcome = service.run_route(task.task_id, self.root)
        action_id = outcome.attempts[0].action_id

        status, output, _ = self.invoke("result", "preview", task.task_id, action_id)
        self.assertEqual(status, 0)
        preview = json.loads(output)
        self.assertEqual(preview["changes"]["summary_after"], "Fresh CLI summary")

        status, output, _ = self.invoke(
            "result",
            "accept",
            task.task_id,
            action_id,
            "--expected-revision",
            str(preview["checkpoint_revision"]),
        )
        self.assertEqual(status, 0)
        accepted = json.loads(output)["task"]
        self.assertEqual(accepted["state"]["summary"], "Fresh CLI summary")
        self.assertEqual(accepted["state"]["tests"], ["cli test passed"])


if __name__ == "__main__":
    unittest.main()
