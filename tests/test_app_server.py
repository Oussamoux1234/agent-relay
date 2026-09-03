from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_relay.app_server import CodexAppServerAdapter
from agent_relay.cli import main
from agent_relay.errors import ValidationError
from agent_relay.models import AgentSpec
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore


FAKE_APP_SERVER = r"""
import json
import sys
import time

mode = sys.argv[1]
log_path = sys.argv[2]

def receive():
    message = json.loads(sys.stdin.readline())
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(message) + "\n")
    return message

def send(message):
    print(json.dumps(message), flush=True)

initialize = receive()
assert initialize["method"] == "initialize"
assert initialize["params"]["clientInfo"]["name"] == "agent_relay"
assert "capabilities" not in initialize["params"]
if mode == "timeout":
    time.sleep(5)
    raise SystemExit(0)
if mode == "invalid-json":
    print("{not-json", flush=True)
    time.sleep(5)
    raise SystemExit(0)
send({"id": initialize["id"], "result": {"serverInfo": {"name": "fake"}}})

initialized = receive()
assert initialized == {"method": "initialized"}
thread_request = receive()
assert thread_request["method"] in ("thread/start", "thread/resume")
workspace_write = mode == "workspace-write"
expected_approval = "on-request" if workspace_write else "never"
expected_sandbox = "workspace-write" if workspace_write else "read-only"
assert thread_request["params"]["approvalPolicy"] == expected_approval
assert thread_request["params"]["approvalsReviewer"] == "user"
assert thread_request["params"]["sandbox"] == expected_sandbox
thread_id = thread_request["params"].get("threadId", "thread-created")
send({"id": thread_request["id"], "result": {"thread": {"id": thread_id}}})

turn_request = receive()
assert turn_request["method"] == "turn/start"
assert turn_request["params"]["threadId"] == thread_id
assert turn_request["params"]["approvalPolicy"] == expected_approval
assert turn_request["params"]["approvalsReviewer"] == "user"
if workspace_write:
    assert turn_request["params"]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(__import__("pathlib").Path.cwd())],
        "readOnlyAccess": {
            "type": "restricted",
            "includePlatformDefaults": True,
            "readableRoots": [str(__import__("pathlib").Path.cwd())],
        },
        "networkAccess": False,
        "excludeTmpdirEnvVar": True,
        "excludeSlashTmp": True,
    }
else:
    assert turn_request["params"]["sandboxPolicy"] == {
        "type": "readOnly",
        "access": {
            "type": "restricted",
            "includePlatformDefaults": True,
            "readableRoots": [str(__import__("pathlib").Path.cwd())],
        },
        "networkAccess": False,
    }
prompt = turn_request["params"]["input"][0]["text"]
payload = json.loads(prompt[prompt.index("{"):])
contract = payload["handoff"]["result_contract"]
result = dict(contract["schema"])
result["summary"] = "Codex App checkpoint returned"
result["tests"] = ["fake app-server protocol passed"]
answer = "Visible answer that is not ledger memory\n%s\n%s\n%s" % (
    contract["begin_marker"], json.dumps(result), contract["end_marker"]
)

send({"id": turn_request["id"], "result": {"turn": {"id": "turn-created"}}})
if mode == "approval":
    send({
        "id": 99,
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": thread_id, "turnId": "turn-created"},
    })
    decision = receive()
    assert decision == {"id": 99, "result": {"decision": "decline"}}
send({
    "method": "item/completed",
    "params": {
        "threadId": thread_id,
        "turnId": "turn-created",
        "completedAtMs": 1,
        "item": {"type": "reasoning", "id": "private", "summary": ["private reasoning"]},
    },
})
send({
    "method": "item/completed",
    "params": {
        "threadId": thread_id,
        "turnId": "turn-created",
        "completedAtMs": 2,
        "item": {"type": "agentMessage", "id": "answer", "text": answer},
    },
})
status = "failed" if mode == "failed-turn" else "completed"
send({
    "method": "turn/completed",
    "params": {
        "threadId": thread_id,
        "turn": {"id": "turn-created", "status": status, "items": []},
    },
})
time.sleep(5)
"""


class CodexAppServerAdapterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log_path = self.root / "protocol.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def spec(self, mode: str = "success", timeout_seconds: int = 5) -> AgentSpec:
        is_workspace_write = mode == "workspace-write"
        return AgentSpec(
            agent_id="codex-app",
            display_name="Codex App",
            command=(
                sys.executable,
                "-u",
                "-c",
                FAKE_APP_SERVER,
                mode,
                str(self.log_path),
            ),
            adapter_type="codex-app-server",
            prompt_transport="stdin",
            timeout_seconds=timeout_seconds,
            capabilities=(
                ("repo-read", "repo-write") if is_workspace_write else ("repo-read",)
            ),
            provider_id=(
                "codex-app-server-write" if is_workspace_write else "codex-app-server"
            ),
            permission_profile=("workspace-write" if is_workspace_write else "read-only"),
        )

    @staticmethod
    def prompt(task_id: str = "task123", action_id: str = "action123") -> str:
        return json.dumps(
            {
                "handoff": {
                    "result_contract": {
                        "begin_marker": "<<<AGENT_RELAY_RESULT>>>",
                        "end_marker": "<<<END_AGENT_RELAY_RESULT>>>",
                        "schema": {
                            "schema_version": "1.0",
                            "task_id": task_id,
                            "source_action_id": action_id,
                            "summary": "required",
                            "decisions": [],
                            "constraints": [],
                            "files_changed": [],
                            "tests": [],
                            "next_steps": [],
                        },
                    }
                }
            }
        )

    def messages(self):
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_starts_read_only_thread_and_returns_only_agent_message(self) -> None:
        result = CodexAppServerAdapter().execute(
            self.spec(),
            self.prompt(),
            self.root,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.session_id, "thread-created")
        self.assertEqual(result.turn_id, "turn-created")
        self.assertEqual(result.protocol_status, "completed")
        self.assertIn("Visible answer", result.stdout)
        self.assertNotIn("private reasoning", result.stdout)
        self.assertIn("item/completed", result.event_types)
        self.assertIn("turn/completed", result.event_types)
        requests = self.messages()
        self.assertEqual(requests[2]["method"], "thread/start")
        self.assertEqual(requests[3]["method"], "turn/start")

        policy = requests[3]["params"]["sandboxPolicy"]
        self.assertEqual(
            policy["access"],
            {
                "type": "restricted",
                "includePlatformDefaults": True,
                "readableRoots": [str(self.root.resolve())],
            },
        )

    def test_resumes_explicit_thread_and_declines_command_approval(self) -> None:
        result = CodexAppServerAdapter().execute_session(
            self.spec("approval"),
            self.prompt(),
            self.root,
            "thread-existing",
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.session_id, "thread-existing")
        requests = self.messages()
        self.assertEqual(requests[2]["method"], "thread/resume")
        self.assertEqual(requests[2]["params"]["threadId"], "thread-existing")
        self.assertEqual(requests[-1]["result"]["decision"], "decline")

    def test_workspace_write_uses_one_root_without_network_or_temp_access(self) -> None:
        result = CodexAppServerAdapter().execute(
            self.spec("workspace-write"),
            self.prompt(),
            self.root,
        )

        self.assertEqual(result.status, "completed")
        requests = self.messages()
        self.assertEqual(requests[2]["params"]["approvalPolicy"], "on-request")
        policy = requests[3]["params"]["sandboxPolicy"]
        self.assertEqual(policy["writableRoots"], [str(self.root.resolve())])
        self.assertEqual(
            policy["readOnlyAccess"],
            {
                "type": "restricted",
                "includePlatformDefaults": True,
                "readableRoots": [str(self.root.resolve())],
            },
        )
        self.assertFalse(policy["networkAccess"])
        self.assertTrue(policy["excludeTmpdirEnvVar"])
        self.assertTrue(policy["excludeSlashTmp"])

    def test_timeout_malformed_protocol_and_failed_turn_are_unknown(self) -> None:
        timeout = CodexAppServerAdapter().execute(
            self.spec("timeout", timeout_seconds=1),
            self.prompt(),
            self.root,
        )
        failed = CodexAppServerAdapter().execute(
            self.spec("failed-turn"),
            self.prompt(),
            self.root,
        )
        malformed = CodexAppServerAdapter().execute(
            self.spec("invalid-json"),
            self.prompt(),
            self.root,
        )

        self.assertEqual(timeout.status, "unknown")
        self.assertTrue(timeout.timed_out)
        self.assertEqual(failed.status, "unknown")
        self.assertEqual(failed.protocol_status, "failed")
        self.assertEqual(malformed.status, "unknown")
        self.assertIn("invalid JSONL", malformed.error)

    def test_invalid_thread_id_and_write_capability_fail_before_launch(self) -> None:
        adapter = CodexAppServerAdapter()
        with self.assertRaises(ValidationError):
            adapter.execute_session(self.spec(), self.prompt(), self.root, "bad\nthread")
        unsafe = AgentSpec(
            agent_id="codex-app-write",
            display_name="Unsafe",
            command=(sys.executable, "-c", "print('launched')"),
            adapter_type="codex-app-server",
            capabilities=("repo-write",),
        )
        with self.assertRaises(ValidationError):
            adapter.execute(unsafe, self.prompt(), self.root)
        self.assertFalse(self.log_path.exists())


class CodexAppServerServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RelayStore(self.root / "state")
        self.service = RelayService(self.store)
        self.log_path = self.root / "service-protocol.jsonl"
        self.service.register_agent(
            AgentSpec(
                agent_id="source",
                display_name="Source",
                command=(sys.executable, "-c", "print('source')"),
            )
        )
        self.service.register_agent(
            AgentSpec(
                agent_id="backup",
                display_name="Backup",
                command=(sys.executable, "-c", "print('backup')"),
            )
        )
        self.service.register_agent(
            AgentSpec(
                agent_id="codex-app",
                display_name="Codex App",
                command=(
                    sys.executable,
                    "-u",
                    "-c",
                    FAKE_APP_SERVER,
                    "success",
                    str(self.log_path),
                ),
                adapter_type="codex-app-server",
                capabilities=("repo-read",),
                provider_id="codex-app-server",
                timeout_seconds=5,
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_app_handoff_audits_safe_metadata_and_proposes_explicit_memory(self) -> None:
        task = self.service.create_task(
            "App connector",
            "Transfer only the explicit checkpoint",
            active_agent="source",
        )

        outcome = self.service.handoff(task.task_id, "codex-app", self.root)

        self.assertEqual(outcome.result_status, "pending")
        self.assertEqual(outcome.result.summary, "Codex App checkpoint returned")
        action = outcome.task.actions[-1]
        self.assertEqual(action.kind, "session-turn")
        self.assertEqual(action.details["external_session_id"], "thread-created")
        self.assertEqual(action.details["external_turn_id"], "turn-created")
        self.assertEqual(action.details["result_status"], "pending")
        persisted = (
            self.root / "state" / "tasks" / (task.task_id + ".json")
        ).read_text(encoding="utf-8")
        self.assertNotIn("private reasoning", persisted)
        self.assertNotIn("Visible answer that is not ledger memory", persisted)

        preview = self.service.preview_result(task.task_id, outcome.action_id)
        accepted = self.service.accept_result(
            task.task_id,
            outcome.action_id,
            preview.task.revision,
        )
        self.assertEqual(accepted.state.summary, "Codex App checkpoint returned")

    def test_later_handoff_to_same_app_resumes_recorded_thread(self) -> None:
        task = self.service.create_task("Resume", "Keep app continuity", active_agent="source")
        first = self.service.handoff(task.task_id, "codex-app", self.root)
        self.service.handoff(task.task_id, "backup", self.root)

        second = self.service.handoff(task.task_id, "codex-app", self.root)

        self.assertEqual(first.execution.session_id, "thread-created")
        self.assertEqual(second.execution.session_id, "thread-created")
        requests = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]
        thread_requests = [
            message for message in requests if message.get("method") in {"thread/start", "thread/resume"}
        ]
        self.assertEqual(thread_requests[0]["method"], "thread/start")
        self.assertEqual(thread_requests[1]["method"], "thread/resume")

    def test_session_id_is_rejected_for_non_session_adapter_before_ledger_write(self) -> None:
        task = self.service.create_task("Invalid", "Reject session mismatch", active_agent="source")

        with self.assertRaises(ValidationError):
            self.service.handoff(
                task.task_id,
                "backup",
                self.root,
                session_id="thread-existing",
            )

        self.assertEqual(self.store.get_task(task.task_id).actions, [])

    def test_cli_can_resume_an_explicit_codex_thread(self) -> None:
        task = self.service.create_task("CLI app", "Resume explicitly", active_agent="source")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(
                [
                    "--state-dir",
                    str(self.root / "state"),
                    "handoff",
                    task.task_id,
                    "codex-app",
                    "--execute",
                    "--cwd",
                    str(self.root),
                    "--thread-id",
                    "thread-from-desktop",
                ]
            )

        self.assertEqual(status, 0, stderr.getvalue())
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["execution"]["session_id"], "thread-from-desktop")
        self.assertEqual(output["execution"]["protocol_status"], "completed")
        self.assertEqual(output["result_status"], "pending")

    def test_cli_requires_execution_when_thread_id_is_supplied(self) -> None:
        task = self.service.create_task("CLI preview", "Reject ignored flags", active_agent="source")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(
                [
                    "--state-dir",
                    str(self.root / "state"),
                    "handoff",
                    task.task_id,
                    "codex-app",
                    "--thread-id",
                    "thread-from-desktop",
                ]
            )

        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error_type"], "ValidationError")
        self.assertEqual(self.store.get_task(task.task_id).actions, [])


if __name__ == "__main__":
    unittest.main()
