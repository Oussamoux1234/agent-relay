from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from threading import BrokenBarrierError
from typing import Any, Dict

from agent_relay.errors import ConflictError, ValidationError
from agent_relay.health import AgentHealthRecord
from agent_relay.models import AgentSpec, TaskCheckpoint
from agent_relay.storage import RelayStore


class _SynchronizedWriteStore(RelayStore):
    def __init__(self, root: Path, write_barrier: Any) -> None:
        self._write_barrier = write_barrier
        super().__init__(root)

    def _atomic_write(self, path: Path, value: Dict[str, Any]) -> None:
        try:
            self._write_barrier.wait(timeout=1)
        except BrokenBarrierError:
            pass
        super()._atomic_write(path, value)


def _save_task_worker(
    state: str,
    task_id: str,
    summary: str,
    start_barrier: Any,
    write_barrier: Any,
    results: Any,
) -> None:
    try:
        store = _SynchronizedWriteStore(Path(state), write_barrier)
        checkpoint = store.get_task(task_id)
        checkpoint.state.summary = summary
        expected_revision = checkpoint.revision
        start_barrier.wait(timeout=10)
        saved = store.save_task(checkpoint, expected_revision)
        results.put(("saved", saved.revision, saved.state.summary))
    except ConflictError as exc:
        results.put(("conflict", None, str(exc)))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _register_agent_worker(
    state: str,
    agent_id: str,
    display_name: str,
    start_barrier: Any,
    write_barrier: Any,
    results: Any,
) -> None:
    try:
        store = _SynchronizedWriteStore(Path(state), write_barrier)
        start_barrier.wait(timeout=10)
        store.register_agent(AgentSpec(agent_id, display_name, ("agent",)))
        results.put(("saved", agent_id))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _set_health_worker(
    state: str,
    agent_id: str,
    source_action_id: str,
    start_barrier: Any,
    write_barrier: Any,
    results: Any,
) -> None:
    try:
        store = _SynchronizedWriteStore(Path(state), write_barrier)
        record = AgentHealthRecord(
            agent_id=agent_id,
            provider_id="codex-cli",
            category="rate_limited",
            evidence_code="concurrency-test",
            retry_source="default_policy",
            observed_at="2026-09-02T12:00:00Z",
            cooldown_until="2026-09-02T12:01:00Z",
            source_task_id="a" * 32,
            source_action_id=source_action_id,
        )
        start_barrier.wait(timeout=10)
        store.set_agent_health(record)
        results.put(("saved", agent_id))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


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

    def test_rejects_a_symlinked_transaction_lock(self) -> None:
        store = RelayStore(self.root / "state")
        outside = self.root / "outside-lock"
        outside.write_text("do not modify", encoding="utf-8")
        lock_path = store.root / store._LOCK_FILENAME
        lock_path.unlink()
        lock_path.symlink_to(outside)

        with self.assertRaisesRegex(ValidationError, "could not open state lock"):
            store.register_agent(AgentSpec("reader", "Reader", ("reader",)))

        self.assertEqual(outside.read_text(encoding="utf-8"), "do not modify")


class RelayStoreConcurrencyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("cross-process locking requires a fork-capable test platform")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.store = RelayStore(self.state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run_workers(self, target: Any, arguments: Any) -> list:
        context = multiprocessing.get_context("fork")
        start_barrier = context.Barrier(3)
        write_barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=target,
                args=(
                    str(self.state),
                    first,
                    second,
                    start_barrier,
                    write_barrier,
                    results,
                ),
            )
            for first, second in arguments
        ]
        try:
            for process in processes:
                process.start()
            start_barrier.wait(timeout=10)
            for process in processes:
                process.join(timeout=15)
            for process in processes:
                self.assertFalse(process.is_alive(), "concurrent storage worker did not exit")
                self.assertEqual(process.exitcode, 0)
            return [results.get(timeout=2) for _ in processes]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()
            results.join_thread()

    def test_concurrent_task_saves_allow_exactly_one_revision_winner(self) -> None:
        checkpoint = TaskCheckpoint.create("Concurrent task", "Initial summary")
        self.store.create_task(checkpoint)

        results = self._run_workers(
            _save_task_worker,
            ((checkpoint.task_id, "Writer one"), (checkpoint.task_id, "Writer two")),
        )

        self.assertEqual(sorted(result[0] for result in results), ["conflict", "saved"])
        saved = self.store.get_task(checkpoint.task_id)
        self.assertEqual(saved.revision, 2)
        self.assertIn(saved.state.summary, {"Writer one", "Writer two"})

    def test_concurrent_agent_registrations_preserve_both_updates(self) -> None:
        results = self._run_workers(
            _register_agent_worker,
            (("reader-one", "Reader One"), ("reader-two", "Reader Two")),
        )

        self.assertEqual([result[0] for result in results], ["saved", "saved"])
        self.assertEqual(
            [agent.agent_id for agent in self.store.list_agents()],
            ["reader-one", "reader-two"],
        )

    def test_concurrent_health_updates_preserve_both_records(self) -> None:
        results = self._run_workers(
            _set_health_worker,
            (("reader-one", "b" * 32), ("reader-two", "c" * 32)),
        )

        self.assertEqual([result[0] for result in results], ["saved", "saved"])
        self.assertEqual(
            [record.agent_id for record in self.store.list_agent_health()],
            ["reader-one", "reader-two"],
        )


if __name__ == "__main__":
    unittest.main()
