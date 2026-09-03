from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_relay.errors import ConflictError, ValidationError
from agent_relay.models import AgentSpec
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore


class RoutingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RelayStore(self.root / "state")
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        self.service = RelayService(self.store, clock=lambda: self.now)

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

    def test_copilot_rate_limit_switches_to_the_next_read_agent(self) -> None:
        self.register(
            "copilot-primary",
            "import sys; sys.stderr.write(\"You've hit a rate limit\"); "
            "raise SystemExit(1)",
            provider_id="github-copilot",
        )
        self.register("codex-backup", "print('backup')")
        task = self.create_routed_task("copilot-primary", "codex-backup")

        outcome = self.service.run_route(task.task_id, self.root)

        self.assertEqual(
            [item.agent_id for item in outcome.attempts],
            ["copilot-primary", "codex-backup"],
        )
        self.assertEqual(outcome.attempts[0].classification.category, "rate_limited")
        self.assertEqual(
            outcome.attempts[0].classification.evidence_code,
            "copilot-rate-limit-hit",
        )
        self.assertEqual(outcome.task.active_agent, "codex-backup")
        health = self.service.get_agent_health("copilot-primary")
        self.assertEqual(health.category, "rate_limited")
        self.assertEqual(health.evidence_code, "copilot-rate-limit-hit")

    def test_ambiguous_failure_blocks_without_running_backup(self) -> None:
        marker = self.root / "backup-ran"
        self.register(
            "codex-primary",
            "import sys; sys.stderr.write('unexpected failure'); raise SystemExit(2)",
        )
        self.register(
            "claude-backup",
            "from pathlib import Path; Path(%r).write_text('ran')" % str(marker),
            provider_id="claude-code",
        )
        task = self.create_routed_task("codex-primary", "claude-backup")

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

    def test_active_cooldowns_skip_every_agent_without_launching_again(self) -> None:
        marker = self.root / "runs"
        error = (
            "from pathlib import Path; import sys; path=Path(%r); "
            "path.write_text(path.read_text() + 'x' if path.exists() else 'x'); "
            "sys.stderr.write('429 too many requests'); raise SystemExit(1)"
        ) % str(marker)
        self.register("codex-primary", error)
        self.register("codex-backup", error)
        task = self.create_routed_task("codex-primary", "codex-backup")
        self.service.run_route(task.task_id, self.root)
        self.assertEqual(marker.read_text(encoding="utf-8"), "xx")

        status = self.service.inspect_route(task.task_id)

        self.assertEqual(status.candidates, ())
        self.assertEqual(
            tuple(record.agent_id for record in status.skipped),
            ("codex-primary", "codex-backup"),
        )
        with self.assertRaises(ConflictError):
            self.service.run_route(task.task_id, self.root)
        self.assertEqual(marker.read_text(encoding="utf-8"), "xx")

        self.now += timedelta(seconds=61)
        preview = self.service.preview_route(task.task_id)
        self.assertEqual(preview.candidates, ("codex-primary", "codex-backup"))
        self.assertEqual(preview.skipped, ())

    def test_structured_retry_hint_is_persisted_without_raw_output(self) -> None:
        self.register(
            "codex-primary",
            "import sys; sys.stderr.write("
            "'{\"error\":{\"type\":\"rate_limit_error\",\"message\":\"TOP-SECRET\"},'"
            "'\"retry-after\":120}'); raise SystemExit(1)",
        )
        self.register("claude-backup", "print('backup')", provider_id="claude-code")
        task = self.create_routed_task("codex-primary", "claude-backup")

        outcome = self.service.run_route(task.task_id, self.root)
        record = self.service.get_agent_health("codex-primary")

        self.assertEqual(outcome.task.active_agent, "claude-backup")
        self.assertEqual(record.retry_source, "provider_hint")
        self.assertEqual(record.cooldown_until, "2026-09-01T12:02:00Z")
        health_text = self.store.health_path.read_text(encoding="utf-8")
        task_text = (
            self.root / "state" / "tasks" / (task.task_id + ".json")
        ).read_text(encoding="utf-8")
        self.assertNotIn("TOP-SECRET", health_text)
        self.assertNotIn("TOP-SECRET", task_text)

    def test_earlier_agent_requires_expiry_and_explicit_route_recovery(self) -> None:
        self.register(
            "codex-primary",
            "import sys; sys.stderr.write("
            "'{\"type\":\"rate_limit_error\",\"retry-after\":5}'); raise SystemExit(1)",
        )
        self.register("claude-backup", "print('backup')", provider_id="claude-code")
        task = self.create_routed_task("codex-primary", "claude-backup")
        outcome = self.service.run_route(task.task_id, self.root)
        self.assertEqual(outcome.task.active_agent, "claude-backup")

        with self.assertRaises(ConflictError):
            self.service.recover_route(task.task_id, "codex-primary")

        self.now += timedelta(seconds=6)
        preview = self.service.preview_route(task.task_id)
        self.assertEqual(preview.candidates, ("claude-backup",))

        recovered = self.service.recover_route(task.task_id, "codex-primary")

        self.assertEqual(recovered.active_agent, "codex-primary")
        self.assertEqual(recovered.actions[-1].kind, "route-recover")
        self.assertEqual(
            recovered.actions[-1].details["recovery"],
            "explicit-user-command",
        )
        self.assertEqual(
            self.service.preview_route(task.task_id).candidates,
            ("codex-primary", "claude-backup"),
        )

    def test_authentication_and_unknown_failures_do_not_create_cooldowns(self) -> None:
        for primary, message in (
            ("codex-auth", "401 authentication_error"),
            ("codex-unknown", "unexpected failure"),
        ):
            with self.subTest(primary=primary):
                self.register(
                    primary,
                    "import sys; sys.stderr.write(%r); raise SystemExit(1)" % message,
                )
                backup = primary + "-backup"
                self.register(backup, "print('backup')")
                task = self.create_routed_task(primary, backup)

                outcome = self.service.run_route(task.task_id, self.root)

                self.assertEqual(len(outcome.attempts), 1)
                self.assertIsNone(self.store.get_agent_health(primary))

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

    def test_gemini_and_antigravity_are_rejected_from_automatic_routes(self) -> None:
        for provider_id in ("gemini-cli", "antigravity-cli"):
            with self.subTest(provider_id=provider_id):
                suffix = provider_id.removesuffix("-cli")
                primary = "codex-" + suffix
                manual_only = suffix + "-manual"
                self.register(primary, "print('primary')")
                self.register(
                    manual_only,
                    "print('manual only')",
                    provider_id=provider_id,
                )
                task = self.service.create_task(
                    "Task",
                    "Unsafe plan modes cannot enter automatic routes",
                    active_agent=primary,
                )

                with self.assertRaisesRegex(
                    ValidationError,
                    "automatic routing is disabled",
                ):
                    self.service.configure_route(
                        task.task_id,
                        [primary, manual_only],
                    )

                persisted = self.store.get_task(task.task_id)
                self.assertEqual(persisted.routing_order, [])
                self.assertEqual(persisted.actions, [])

    def test_legacy_route_with_manual_only_provider_fails_before_launch(self) -> None:
        marker = self.root / "provider-ran"
        self.register("codex-primary", "print('primary')")
        self.register(
            "gemini-manual",
            "from pathlib import Path; Path(%r).write_text('ran')" % str(marker),
            provider_id="gemini-cli",
        )
        task = self.service.create_task(
            "Task",
            "Existing unsafe routes fail closed after upgrade",
            active_agent="codex-primary",
        )
        task.routing_order = ["codex-primary", "gemini-manual"]
        task.__post_init__()
        self.store.save_task(task, task.revision)

        with self.assertRaisesRegex(
            ValidationError,
            "automatic routing is disabled",
        ):
            self.service.preview_route(task.task_id)

        with self.assertRaisesRegex(
            ValidationError,
            "automatic routing is disabled",
        ):
            self.service.run_route(task.task_id, self.root)

        self.assertFalse(marker.exists())
        self.assertEqual(self.store.get_task(task.task_id).actions, [])

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
