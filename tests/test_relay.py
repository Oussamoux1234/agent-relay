from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_relay.errors import ConflictError, ValidationError
from agent_relay.models import AgentSpec
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore


class RelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RelayStore(self.root / "state")
        self.service = RelayService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(
        self,
        agent_id: str,
        code: str,
        prompt_transport: str = "stdin",
        timeout_seconds: int = 5,
    ) -> AgentSpec:
        spec = AgentSpec(
            agent_id=agent_id,
            display_name=agent_id.title(),
            command=(sys.executable, "-c", code),
            prompt_transport=prompt_transport,
            timeout_seconds=timeout_seconds,
        )
        return self.service.register_agent(spec)

    def test_registers_and_lists_user_owned_agents(self) -> None:
        self.register("gemini-cli", "print('ok')")
        self.register("claude-code", "print('ok')")

        agents = self.store.list_agents()

        self.assertEqual([agent.agent_id for agent in agents], ["claude-code", "gemini-cli"])
        registry_mode = (self.root / "state" / "agents.json").stat().st_mode & 0o777
        self.assertEqual(registry_mode, 0o600)

    def test_rejects_invalid_agent_ids_and_empty_commands(self) -> None:
        with self.assertRaises(ValidationError):
            AgentSpec(agent_id="../../bad", display_name="Bad", command=("bad",))
        with self.assertRaises(ValidationError):
            AgentSpec(agent_id="valid", display_name="Valid", command=())

    def test_creates_and_updates_a_portable_checkpoint(self) -> None:
        self.register("codex", "print('ok')")
        checkpoint = self.service.create_task(
            title="Authentication",
            goal="Add login support",
            active_agent="codex",
        )

        updated = self.service.add_task_notes(
            checkpoint.task_id,
            summary="Login endpoint implemented",
            decisions=["Use signed cookies"],
            files_changed=["app/auth.py"],
            tests=["python3 -m unittest: passed"],
            next_steps=["Add logout"],
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.state.summary, "Login endpoint implemented")
        self.assertIn("app/auth.py", updated.state.files_changed)
        persisted = json.loads(
            (self.root / "state" / "tasks" / (checkpoint.task_id + ".json")).read_text()
        )
        self.assertEqual(persisted["schema_version"], "1.0")

    def test_preview_does_not_change_task_state(self) -> None:
        self.register("codex", "print('ok')")
        self.register("claude-code", "print('ok')")
        task = self.service.create_task("Task", "Continue safely", active_agent="codex")

        outcome = self.service.preview_handoff(task.task_id, "claude-code")

        self.assertTrue(outcome.dry_run)
        self.assertIn('"target_agent": "claude-code"', outcome.prompt)
        self.assertIn("not hidden chain-of-thought", outcome.prompt)
        persisted = self.store.get_task(task.task_id)
        self.assertEqual(persisted.revision, 1)
        self.assertEqual(persisted.actions, [])

    def test_successful_handoff_updates_active_agent_and_ledger(self) -> None:
        self.register("codex", "print('source')")
        self.register(
            "gemini-cli",
            "import sys; prompt = sys.stdin.read(); "
            "print('checkpoint-received=' + str('Continue safely' in prompt))",
        )
        task = self.service.create_task("Task", "Continue safely", active_agent="codex")

        outcome = self.service.handoff(task.task_id, "gemini-cli", self.root)

        self.assertEqual(outcome.execution.status, "completed")
        self.assertIn("checkpoint-received=True", outcome.execution.stdout)
        self.assertEqual(outcome.task.active_agent, "gemini-cli")
        self.assertEqual(outcome.task.actions[-1].status, "completed")
        self.assertEqual(outcome.task.revision, 3)

    def test_nonzero_exit_blocks_until_user_resolves_unknown_effects(self) -> None:
        self.register("codex", "print('source')")
        self.register("custom-agent", "raise SystemExit(7)")
        task = self.service.create_task("Task", "Do work", active_agent="codex")

        outcome = self.service.handoff(task.task_id, "custom-agent", self.root)

        self.assertEqual(outcome.execution.status, "unknown")
        self.assertEqual(outcome.task.status, "blocked")
        self.assertEqual(outcome.task.active_agent, "codex")
        with self.assertRaises(ConflictError):
            self.service.preview_handoff(task.task_id, "custom-agent")

        resolved = self.service.resolve_action(
            task.task_id,
            outcome.action_id,
            "failed",
        )
        self.assertEqual(resolved.status, "active")
        self.assertEqual(resolved.active_agent, "codex")
        self.assertEqual(resolved.actions[-1].status, "failed")

    def test_unlaunchable_agent_is_failed_without_blocking_task(self) -> None:
        self.register("codex", "print('source')")
        self.service.register_agent(
            AgentSpec(
                agent_id="missing-agent",
                display_name="Missing",
                command=(str(self.root / "does-not-exist"),),
                timeout_seconds=5,
            )
        )
        task = self.service.create_task("Task", "Do work", active_agent="codex")

        outcome = self.service.handoff(task.task_id, "missing-agent", self.root)

        self.assertFalse(outcome.execution.started)
        self.assertEqual(outcome.execution.status, "failed")
        self.assertEqual(outcome.task.status, "active")
        self.assertEqual(outcome.task.active_agent, "codex")
        self.assertEqual(outcome.task.actions[-1].status, "failed")

    def test_timeout_is_unknown_and_fail_closed(self) -> None:
        self.register("codex", "print('source')")
        self.register(
            "slow-agent",
            "import time; time.sleep(5)",
            timeout_seconds=1,
        )
        task = self.service.create_task("Task", "Do work", active_agent="codex")

        outcome = self.service.handoff(task.task_id, "slow-agent", self.root)

        self.assertTrue(outcome.execution.timed_out)
        self.assertEqual(outcome.execution.status, "unknown")
        self.assertEqual(outcome.task.status, "blocked")
        self.assertEqual(outcome.task.active_agent, "codex")

    def test_preflight_failure_does_not_create_a_pending_action(self) -> None:
        self.register("codex", "print('source')")
        self.register("gemini-cli", "print('target')")
        task = self.service.create_task("Task", "Do work", active_agent="codex")

        with self.assertRaises(ValidationError):
            self.service.handoff(task.task_id, "gemini-cli", self.root / "missing")

        persisted = self.store.get_task(task.task_id)
        self.assertEqual(persisted.actions, [])
        self.assertEqual(persisted.revision, 1)

    def test_argument_transport_does_not_invoke_a_shell(self) -> None:
        marker = self.root / "injected"
        self.register("codex", "print('source')")
        self.register(
            "argument-agent",
            "import sys; print(sys.argv[1])",
            prompt_transport="argument",
        )
        task = self.service.create_task(
            "Task",
            "Do not execute $(touch %s)" % marker,
            active_agent="codex",
        )

        outcome = self.service.handoff(task.task_id, "argument-agent", self.root)

        self.assertEqual(outcome.execution.status, "completed")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
