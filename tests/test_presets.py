from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_relay.cli import main
from agent_relay.errors import NotFoundError, ValidationError
from agent_relay.models import AgentSpec
from agent_relay.presets import PRESETS, build_preset, list_preset_statuses
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore


EXPECTED_ARGUMENTS = {
    "antigravity-cli": (
        "--mode=plan",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ),
    "claude-code": (
        "-p",
        "Continue the task using the Agent Relay checkpoint provided on stdin. "
        "Analyze the repository and return the next response without modifying files.",
        "--permission-mode",
        "plan",
        "--output-format",
        "json",
    ),
    "codex-cli": (
        "exec",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--color",
        "never",
        "-",
    ),
    "gemini-cli": (
        "--approval-mode",
        "plan",
        "--output-format",
        "json",
    ),
    "github-copilot": ("-s", "--available-tools=read", "--disable-builtin-mcps"),
}


class ProviderPresetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RelayStore(self.root / "state")
        self.service = RelayService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_fake_provider(self, preset_id: str) -> Path:
        preset = PRESETS[preset_id]
        executable = self.root / preset.executable_name
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "expected = %r\n" % list(EXPECTED_ARGUMENTS[preset_id])
            + "config_environment = %r\n" % preset.config_home_environment
            + "assert sys.argv[1:] == expected\n"
            + "assert all('portable checkpoint reaches' not in item for item in sys.argv)\n"
            + "raw = sys.stdin.read()\n"
            + (
                "message = json.loads(raw)\n"
                "assert message['event'] == 'user'\n"
                "prompt = message['message']['content']\n"
                if preset.adapter_type == "antigravity-cli"
                else "prompt = raw\n"
            )
            + "config_home = os.environ.get(config_environment) if config_environment else None\n"
            + "print(json.dumps({'received': 'portable checkpoint reaches' in prompt, "
            + "'config_home': config_home}))\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        return executable

    def test_presets_report_missing_executables_without_installing(self) -> None:
        with patch("agent_relay.presets.shutil.which", return_value=None):
            statuses = {item["preset_id"]: item for item in list_preset_statuses()}
            with self.assertRaises(ValidationError):
                build_preset("claude-code")

        self.assertEqual(set(statuses), set(EXPECTED_ARGUMENTS))
        for status in statuses.values():
            self.assertFalse(status["available"])
            self.assertIsNone(status["executable"])

    def test_presets_have_reviewed_analysis_only_invocations(self) -> None:
        for preset_id, expected_arguments in EXPECTED_ARGUMENTS.items():
            with self.subTest(preset_id=preset_id):
                executable = self.make_fake_provider(preset_id)
                spec = build_preset(preset_id, executable=str(executable))

                self.assertEqual(spec.adapter_type, PRESETS[preset_id].adapter_type)
                self.assertEqual(
                    spec.command,
                    (str(executable.resolve()),) + expected_arguments,
                )
                self.assertEqual(spec.prompt_transport, "stdin")
                self.assertEqual(spec.capabilities, ("repo-read",))
                self.assertNotIn("bypassPermissions", spec.command)
                self.assertNotIn("--yolo", spec.command)
                self.assertNotIn("--dangerously-skip-permissions", spec.command)

    def test_provider_shaped_handoffs_complete_through_registry(self) -> None:
        self.service.register_agent(
            AgentSpec(
                agent_id="source",
                display_name="Source",
                command=(sys.executable, "-c", "print('source')"),
            )
        )

        for preset_id in EXPECTED_ARGUMENTS:
            with self.subTest(preset_id=preset_id):
                executable = self.make_fake_provider(preset_id)
                self.service.register_agent(
                    build_preset(preset_id, executable=str(executable))
                )
                task = self.service.create_task(
                    "%s integration" % preset_id,
                    "Prove a portable checkpoint reaches every provider",
                    active_agent="source",
                )

                outcome = self.service.handoff(task.task_id, preset_id, self.root)

                self.assertEqual(outcome.execution.status, "completed")
                self.assertTrue(json.loads(outcome.execution.stdout)["received"])
                self.assertEqual(outcome.task.active_agent, preset_id)
                self.assertEqual(outcome.task.actions[-1].status, "completed")

    def test_antigravity_transport_rejects_argument_prompts(self) -> None:
        self.service.register_agent(
            AgentSpec(
                agent_id="source",
                display_name="Source",
                command=(sys.executable, "-c", "print('source')"),
            )
        )
        executable = self.make_fake_provider("antigravity-cli")
        self.service.register_agent(
            AgentSpec(
                agent_id="antigravity-argument",
                display_name="Invalid Antigravity",
                command=(str(executable),),
                prompt_transport="argument",
                adapter_type="antigravity-cli",
            )
        )
        task = self.service.create_task("Task", "Stay safe", active_agent="source")

        with self.assertRaises(ValidationError):
            self.service.handoff(task.task_id, "antigravity-argument", self.root)

        self.assertEqual(self.store.get_task(task.task_id).actions, [])

    def test_two_instances_of_codex_and_claude_can_switch_with_isolated_config(self) -> None:
        cases = (
            ("codex-cli", "codex-primary", "codex-backup"),
            ("claude-code", "claude-primary", "claude-backup"),
        )
        for preset_id, primary_id, backup_id in cases:
            with self.subTest(preset_id=preset_id):
                executable = self.make_fake_provider(preset_id)
                primary_home = self.root / (primary_id + "-config")
                backup_home = self.root / (backup_id + "-config")
                self.service.register_agent(
                    build_preset(
                        preset_id,
                        agent_id=primary_id,
                        executable=str(executable),
                        config_home=str(primary_home),
                    )
                )
                self.service.register_agent(
                    build_preset(
                        preset_id,
                        agent_id=backup_id,
                        executable=str(executable),
                        config_home=str(backup_home),
                    )
                )
                task = self.service.create_task(
                    "%s failover" % preset_id,
                    "Prove a portable checkpoint reaches every provider",
                    active_agent=primary_id,
                )

                outcome = self.service.handoff(task.task_id, backup_id, self.root)

                response = json.loads(outcome.execution.stdout)
                self.assertEqual(outcome.execution.status, "completed")
                self.assertEqual(response["config_home"], str(backup_home.resolve()))
                self.assertEqual(outcome.task.active_agent, backup_id)

    def test_config_home_is_restricted_to_supported_presets_and_absolute_paths(self) -> None:
        executable = self.make_fake_provider("gemini-cli")
        with self.assertRaises(ValidationError):
            build_preset(
                "gemini-cli",
                executable=str(executable),
                config_home=str(self.root / "gemini-config"),
            )

        executable = self.make_fake_provider("codex-cli")
        with self.assertRaises(ValidationError):
            build_preset(
                "codex-cli",
                executable=str(executable),
                config_home="relative/config",
            )
        with self.assertRaises(ValidationError):
            AgentSpec(
                agent_id="unsafe",
                display_name="Unsafe",
                command=("unsafe",),
                config_home=("OPENAI_API_KEY", str(self.root)),
            )

    def test_unknown_runtime_type_cannot_be_registered(self) -> None:
        spec = AgentSpec(
            agent_id="future-agent",
            display_name="Future Agent",
            command=("future",),
            adapter_type="future-app",
        )

        with self.assertRaises(NotFoundError):
            self.service.register_agent(spec)

    def test_existing_registry_without_adapter_type_remains_compatible(self) -> None:
        registry = {
            "schema_version": "1.0",
            "agents": {
                "legacy": {
                    "agent_id": "legacy",
                    "display_name": "Legacy",
                    "command": ["legacy"],
                    "prompt_transport": "stdin",
                    "timeout_seconds": 30,
                    "capabilities": [],
                    "env_allowlist": [],
                }
            },
        }
        self.store.registry_path.write_text(json.dumps(registry), encoding="utf-8")

        loaded = self.store.get_agent("legacy")

        self.assertEqual(loaded.adapter_type, "cli")

    def test_agent_spec_positional_contract_remains_compatible(self) -> None:
        spec = AgentSpec("legacy", "Legacy", ("legacy",), "argument", 30, (), ())

        self.assertEqual(spec.prompt_transport, "argument")
        self.assertEqual(spec.timeout_seconds, 30)
        self.assertEqual(spec.adapter_type, "cli")

    def test_cli_can_register_provider_preset_with_explicit_executable(self) -> None:
        executable = self.make_fake_provider("codex-cli")
        config_home = self.root / "codex-secondary-config"

        with contextlib.redirect_stdout(io.StringIO()):
            status = main(
                [
                    "--state-dir",
                    str(self.root / "cli-state"),
                    "agent",
                    "add-preset",
                    "codex-cli",
                    "--executable",
                    str(executable),
                    "--config-home",
                    str(config_home),
                ]
            )

        self.assertEqual(status, 0)
        registered = RelayStore(self.root / "cli-state").get_agent("codex-cli")
        self.assertEqual(
            registered.command,
            (str(executable.resolve()),) + EXPECTED_ARGUMENTS["codex-cli"],
        )
        self.assertEqual(registered.adapter_type, "cli")
        self.assertEqual(
            registered.config_home,
            ("CODEX_HOME", str(config_home.resolve())),
        )


if __name__ == "__main__":
    unittest.main()
