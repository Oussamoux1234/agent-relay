from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_relay.errors import ValidationError
from agent_relay.models import AgentSpec, TaskCheckpoint
from agent_relay.storage import RelayStore


class RelayStoreSecurityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_rejects_a_symlink_passed_as_the_state_root(self) -> None:
        target = self.root / "target"
        target.mkdir()
        target.chmod(0o755)
        link = self.root / "state"
        link.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(ValidationError, "state root must not be a symlink"):
            RelayStore(link)

        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_rejects_tasks_directory_symlink_without_chmod_or_external_write(self) -> None:
        state = self.root / "state"
        outside = self.root / "outside"
        state.mkdir()
        outside.mkdir()
        outside.chmod(0o755)
        (state / "tasks").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValidationError, "state directory must not be a symlink"):
            RelayStore(state)

        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_tasks_directory_replaced_by_a_symlink_after_initialization(self) -> None:
        state = self.root / "state"
        outside = self.root / "outside"
        outside.mkdir()
        outside.chmod(0o755)
        store = RelayStore(state)
        store.tasks_dir.rmdir()
        store.tasks_dir.symlink_to(outside, target_is_directory=True)
        checkpoint = TaskCheckpoint.create("Escape", "Do not write outside state")

        with self.assertRaisesRegex(ValidationError, "state directory must not be a symlink"):
            store.create_task(checkpoint)

        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_managed_directories_replaced_by_different_inodes(self) -> None:
        state = self.root / "state"
        store = RelayStore(state)
        original_tasks = self.root / "original-tasks"
        store.tasks_dir.rename(original_tasks)
        store.tasks_dir.mkdir()
        checkpoint = TaskCheckpoint.create("Replacement", "Reject a replacement directory")

        with self.assertRaisesRegex(ValidationError, "managed state directory was replaced"):
            store.create_task(checkpoint)

        self.assertEqual(list(store.tasks_dir.iterdir()), [])
        self.assertEqual(list(original_tasks.iterdir()), [])

    def test_rejects_state_root_replaced_by_a_different_inode(self) -> None:
        state = self.root / "state"
        store = RelayStore(state)
        original_state = self.root / "original-state"
        state.rename(original_state)
        state.mkdir()

        with self.assertRaisesRegex(ValidationError, "managed state directory was replaced"):
            store.list_agents()

        self.assertEqual(list(state.iterdir()), [])

    def test_rejects_registry_and_health_file_symlinks(self) -> None:
        store = RelayStore(self.root / "state")
        outside_registry = self.root / "outside-agents.json"
        outside_health = self.root / "outside-health.json"
        registry_payload = {"schema_version": "1.0", "agents": {}}
        health_payload = {"schema_version": "1.0", "agents": {}}
        outside_registry.write_text(json.dumps(registry_payload), encoding="utf-8")
        outside_health.write_text(json.dumps(health_payload), encoding="utf-8")
        store.registry_path.symlink_to(outside_registry)
        store.health_path.symlink_to(outside_health)

        with self.assertRaisesRegex(ValidationError, "regular file, not a symlink"):
            store.register_agent(AgentSpec("reader", "Reader", ("reader",)))
        with self.assertRaisesRegex(ValidationError, "regular file, not a symlink"):
            store.list_agent_health()

        self.assertEqual(json.loads(outside_registry.read_text()), registry_payload)
        self.assertEqual(json.loads(outside_health.read_text()), health_payload)

    def test_rejects_task_file_symlinks_for_reads_writes_and_listing(self) -> None:
        store = RelayStore(self.root / "state")
        checkpoint = TaskCheckpoint.create("Linked task", "Reject redirected state")
        outside = self.root / "outside-task.json"
        original = json.dumps(checkpoint.to_dict(), sort_keys=True)
        outside.write_text(original, encoding="utf-8")
        task_path = store.tasks_dir / (checkpoint.task_id + ".json")
        task_path.symlink_to(outside)

        with self.assertRaisesRegex(ValidationError, "regular file, not a symlink"):
            store.get_task(checkpoint.task_id)
        with self.assertRaisesRegex(ValidationError, "regular file, not a symlink"):
            store.create_task(checkpoint)
        with self.assertRaisesRegex(ValidationError, "regular file, not a symlink"):
            store.list_tasks()

        self.assertEqual(outside.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
