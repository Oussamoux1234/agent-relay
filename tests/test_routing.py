from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_relay.errors import ValidationError
from agent_relay.models import AgentSpec
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore


class RoutingTestCase(unittest.TestCase):
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
        provider_id: str = "codex-cli",
    ) -> AgentSpec:
        return self.service.register_agent(
            AgentSpec(
                agent_id=agent_id,
                display_name=agent_id.title(),
                command=(sys.executable, "-c", code),
                timeout_seconds=5,
                capabilities=("repo-read",),
                provider_id=provider_id,
            )
        )

    def create_routed_task(self, primary: str, backup: str):
        task = self.service.create_task("Task", "Continue safely", active_agent=primary)
        return self.service.configure_route(task.task_id, [primary, backup])

    def test_clear_rate_limit_switches_to_backup_with_shared_checkpoint(self) -> None:
        self.register(
            "codex-primary",
            "import sys; sys.stderr.write('HTTP 429 rate_limit_error'); raise SystemExit(1)",
        )
        self.register(
            "claude-backup",
            "import sys; prompt = sys.stdin.read(); "
            "print('checkpoint=' + str('Continue safely' in prompt))",
            provider_id="claude-code",
        )
        task = self.create_routed_task("codex-primary", "claude-backup")

        outcome = self.service.run_route(task.task_id, self.root)

        self.assertEqual([item.agent_id for item in outcome.attempts], [
            "codex-primary",
            "claude-backup",
        ])
        self.assertEqual(outcome.attempts[0].classification.category, "rate_limited")
        self.assertEqual(outcome.attempts[1].classification.category, "success")
        self.assertIn("checkpoint=True", outcome.attempts[1].execution.stdout)
        self.assertEqual(outcome.task.active_agent, "claude-backup")
        self.assertEqual(outcome.task.status, "active")
        self.assertEqual([item.status for item in outcome.task.actions], ["failed", "completed"])
        persisted_text = (
            self.root / "state" / "tasks" / (task.task_id + ".json")
        ).read_text(encoding="utf-8")
        self.assertNotIn("rate_limit_error", persisted_text)

    def test_ambiguous_failure_blocks_without_running_backup(self) -> None:
        marker = self.root / "backup-ran"
        self.register(
            "codex-primary",
            "import sys; sys.stderr.write('unexpected failure'); raise SystemExit(2)",
        )
        self.register(
            "gemini-backup",
            "from pathlib import Path; Path(%r).write_text('ran')" % str(marker),
            provider_id="gemini-cli",
        )
        task = self.create_routed_task("codex-primary", "gemini-backup")

        outcome = self.service.run_route(task.task_id, self.root)

        self.assertEqual(len(outcome.attempts), 1)
        self.assertEqual(outcome.attempts[0].classification.category, "unknown")
        self.assertEqual(outcome.task.status, "blocked")
        self.assertEqual(outcome.task.actions[-1].status, "unknown")
        self.assertFalse(marker.exists())

    def test_unlaunchable_primary_is_skipped(self) -> None:
        self.service.register_agent(
            AgentSpec(
                agent_id="codex-primary",
                display_name="Missing Codex",
                command=(str(self.root / "missing-codex"),),
                timeout_seconds=5,
                capabilities=("repo-read",),
                provider_id="codex-cli",
            )
        )
        self.register("codex-backup", "print('backup')")
        task = self.create_routed_task("codex-primary", "codex-backup")

        outcome = self.service.run_route(task.task_id, self.root)

        self.assertEqual(len(outcome.attempts), 2)
        self.assertEqual(outcome.attempts[0].classification.category, "unavailable")
        self.assertEqual(outcome.task.active_agent, "codex-backup")

    def test_all_limited_agents_leave_task_blocked_without_unknown_actions(self) -> None:
        error = "import sys; sys.stderr.write('429 too many requests'); raise SystemExit(1)"
        self.register("codex-primary", error)
        self.register("codex-backup", error)
        task = self.create_routed_task("codex-primary", "codex-backup")

        outcome = self.service.run_route(task.task_id, self.root)

        self.assertEqual(len(outcome.attempts), 2)
        self.assertEqual(outcome.task.status, "blocked")
        self.assertEqual(outcome.task.active_agent, "codex-primary")
        self.assertEqual(outcome.task.unresolved_actions(), [])
        self.assertEqual([item.status for item in outcome.task.actions], ["failed", "failed"])

    def test_route_preview_is_read_only(self) -> None:
        self.register("codex-primary", "print('primary')")
        self.register("codex-backup", "print('backup')")
        task = self.create_routed_task("codex-primary", "codex-backup")

        outcome = self.service.preview_route(task.task_id)

        self.assertTrue(outcome.dry_run)
        self.assertEqual(outcome.candidates, ("codex-primary", "codex-backup"))
        self.assertIn('"routing_order"', outcome.prompt)
        persisted = self.store.get_task(task.task_id)
        self.assertEqual(persisted.revision, task.revision)
        self.assertEqual(persisted.actions, [])

    def test_route_rejects_generic_unsafe_or_invalid_orders(self) -> None:
        self.register("codex-primary", "print('primary')")
        self.service.register_agent(
            AgentSpec(
                agent_id="generic",
                display_name="Generic",
                command=(sys.executable, "-c", "print('generic')"),
            )
        )
        task = self.service.create_task("Task", "Safe route", active_agent="codex-primary")

        with self.assertRaises(ValidationError):
            self.service.configure_route(task.task_id, ["codex-primary", "generic"])
        with self.assertRaises(ValidationError):
            self.service.configure_route(task.task_id, ["codex-primary", "codex-primary"])
        with self.assertRaises(ValidationError):
            self.service.configure_route(task.task_id, ["generic", "codex-primary"])

    def test_old_checkpoint_without_routing_order_remains_compatible(self) -> None:
        self.register("codex-primary", "print('primary')")
        task = self.service.create_task("Task", "Compatibility", active_agent="codex-primary")
        task_path = self.root / "state" / "tasks" / (task.task_id + ".json")
        value = json.loads(task_path.read_text(encoding="utf-8"))
        value.pop("routing_order")
        task_path.write_text(json.dumps(value), encoding="utf-8")

        loaded = self.store.get_task(task.task_id)

        self.assertEqual(loaded.routing_order, [])


if __name__ == "__main__":
    unittest.main()
