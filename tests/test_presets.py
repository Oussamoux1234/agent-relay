from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_relay.adapters import CopilotCliAdapter
from agent_relay.cli import main
from agent_relay.errors import NotFoundError, ValidationError
from agent_relay.models import AgentSpec
from agent_relay.presets import PRESETS, build_preset, list_preset_statuses
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore


CODEX_EXPECTED_CONTAINMENT_ARGUMENTS = (
    "-c",
    'web_search="disabled"',
    "-c",
    "mcp_servers={}",
    "--disable",
    "apps",
    "--disable",
    "plugins",
    "--disable",
    "remote_plugin",
    "--disable",
    "hooks",
    "--disable",
    "browser_use",
    "--disable",
    "browser_use_external",
    "--disable",
    "browser_use_full_cdp_access",
    "--disable",
    "computer_use",
    "--disable",
    "in_app_browser",
    "--disable",
    "image_generation",
    "--disable",
    "skill_mcp_dependency_install",
    "--disable",
    "tool_suggest",
)

CODEX_EXPECTED_READ_PERMISSION_ARGUMENTS = (
    "-c",
    'default_permissions="agent-relay-read"',
    "-c",
    'permissions.agent-relay-read={filesystem={":minimal"="read",'
    '":workspace_roots"={"."="read"}},network={enabled=false}}',
)

CODEX_EXPECTED_WRITE_PERMISSION_ARGUMENTS = (
    "-c",
    'default_permissions="agent-relay-write"',
    "-c",
    'permissions.agent-relay-write={extends=":workspace",filesystem={'
    '":root"="deny",":minimal"="read",":tmpdir"="deny",'
    '":slash_tmp"="deny",":workspace_roots"={"."="write"}},'
    "network={enabled=false}}",
)


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
        "--safe-mode",
        "--restricted",
        "--permission-mode",
        "plan",
        "--tools",
        "Read,Glob,Grep",
        "--disallowedTools",
        "mcp__*",
        "--no-chrome",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "json",
    ),
    "claude-code-write": (
        "-p",
        "Continue the task using the Agent Relay checkpoint provided on stdin. "
        "Make only the requested repository changes inside the current workspace and "
        "return the next response.",
        "--safe-mode",
        "--restricted",
        "--permission-mode",
        "acceptEdits",
        "--tools",
        "Read,Edit,Write,Glob,Grep",
        "--disallowedTools",
        "mcp__*",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "json",
    ),
    "codex-cli": CODEX_EXPECTED_CONTAINMENT_ARGUMENTS
    + CODEX_EXPECTED_READ_PERMISSION_ARGUMENTS
    + (
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--color",
        "never",
        "-",
    ),
    "codex-app-server": CODEX_EXPECTED_CONTAINMENT_ARGUMENTS
    + ("app-server", "--listen", "stdio://"),
    "codex-app-server-write": CODEX_EXPECTED_CONTAINMENT_ARGUMENTS
    + ("app-server", "--listen", "stdio://"),
    "codex-cli-write": (
        "--ask-for-approval",
        "on-request",
    )
    + CODEX_EXPECTED_CONTAINMENT_ARGUMENTS
    + CODEX_EXPECTED_WRITE_PERMISSION_ARGUMENTS
    + (
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
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
    "github-copilot": (
        "-s",
        "--available-tools=view,glob,grep",
        "--deny-tool=write,create,edit,shell,powershell,url,memory,task,web_fetch",
        "--disable-builtin-mcps",
        "--no-custom-instructions",
        "--experimental",
        "--sandbox",
        "--no-remote",
        "--no-remote-export",
        "--disallow-temp-dir",
        "--no-ask-user",
        "--no-auto-update",
        "--no-bash-env",
        "--no-color",
        "--log-level=none",
        "--output-format=json",
    ),
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
            + "settings = {}\n"
            + "auth_state_present = False\n"
            + "auth_state_keys = []\n"
            + "if config_home:\n"
            + "    settings_path = os.path.join(config_home, 'settings.json')\n"
            + "    if os.path.isfile(settings_path):\n"
            + "        with open(settings_path, encoding='utf-8') as handle:\n"
            + "            settings = json.load(handle)\n"
            + "    auth_path = os.path.join(config_home, 'config.json')\n"
            + "    auth_state_present = os.path.isfile(auth_path)\n"
            + "    if auth_state_present:\n"
            + "        with open(auth_path, encoding='utf-8') as handle:\n"
            + "            auth_state_keys = sorted(json.load(handle))\n"
            + "print(json.dumps({'received': 'portable checkpoint reaches' in prompt, "
            + "'config_home': config_home, 'cache_home': os.environ.get('COPILOT_CACHE_HOME'), "
            + "'settings': settings, 'auth_state_present': auth_state_present, "
            + "'auth_state_keys': auth_state_keys}))\n",
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
        self.assertEqual(statuses["claude-code"]["minimum_version"], "2.1.248")
        self.assertEqual(
            statuses["claude-code-write"]["minimum_version"],
            "2.1.248",
        )
        self.assertEqual(statuses["github-copilot"]["minimum_version"], "1.0.79")
        self.assertIsNone(statuses["codex-cli"]["minimum_version"])

    def test_presets_have_reviewed_permission_bounded_invocations(self) -> None:
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
                self.assertEqual(spec.capabilities, PRESETS[preset_id].capabilities)
                self.assertEqual(
                    spec.permission_profile,
                    PRESETS[preset_id].permission_profile,
                )
                self.assertEqual(spec.provider_id, preset_id)
                self.assertNotIn("bypassPermissions", spec.command)
                self.assertNotIn("--yolo", spec.command)
                self.assertNotIn("--dangerously-skip-permissions", spec.command)

    def test_copilot_preset_exposes_only_repository_read_tools(self) -> None:
        arguments = PRESETS["github-copilot"].fixed_arguments

        self.assertIn("--available-tools=view,glob,grep", arguments)
        self.assertIn(
            "--deny-tool=write,create,edit,shell,powershell,url,memory,task,web_fetch",
            arguments,
        )
        self.assertIn("--disable-builtin-mcps", arguments)
        self.assertIn("--no-custom-instructions", arguments)
        self.assertIn("--experimental", arguments)
        self.assertIn("--sandbox", arguments)
        self.assertIn("--no-remote", arguments)
        self.assertNotIn("--no-experimental", arguments)
        self.assertNotIn("--allow-all", arguments)
        self.assertNotIn("--allow-all-tools", arguments)
        self.assertNotIn("--allow-all-paths", arguments)
        self.assertNotIn("--allow-all-urls", arguments)

    def test_copilot_execution_uses_ephemeral_fail_closed_configuration(self) -> None:
        self.service.register_agent(
            AgentSpec(
                agent_id="source",
                display_name="Source",
                command=(sys.executable, "-c", "print('source')"),
            )
        )
        source_home = self.root / "copilot-source"
        source_home.mkdir()
        (source_home / "config.json").write_text(
            "// Copilot managed state\n"
            + json.dumps(
                {
                    "loggedInUsers": [
                        {"host": "https://github.com", "login": "fixture"}
                    ],
                    "installedPlugins": {"hostile": "/tmp/hostile"},
                    "disableAllHooks": False,
                }
            ),
            encoding="utf-8",
        )
        executable = self.make_fake_provider("github-copilot")
        self.service.register_agent(
            build_preset(
                "github-copilot",
                executable=str(executable),
                config_home=str(source_home),
            )
        )
        task = self.service.create_task(
            "Copilot containment",
            "Prove a portable checkpoint reaches every provider",
            active_agent="source",
        )

        outcome = self.service.handoff(task.task_id, "github-copilot", self.root)

        response = json.loads(outcome.execution.stdout)
        isolated_home = Path(response["config_home"])
        sandbox = response["settings"]["sandbox"]
        self.assertEqual(outcome.execution.status, "completed")
        self.assertNotEqual(isolated_home, source_home)
        self.assertFalse(isolated_home.exists())
        self.assertTrue(response["auth_state_present"])
        self.assertEqual(response["auth_state_keys"], ["loggedInUsers"])
        self.assertEqual(response["cache_home"], str(isolated_home / "cache"))
        self.assertTrue(response["settings"]["disableAllHooks"])
        self.assertEqual(
            response["settings"]["permissions"]["disableBypassPermissionsMode"],
            "disable",
        )
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowBypass"])
        self.assertFalse(sandbox["addCurrentWorkingDirectory"])
        self.assertFalse(sandbox["allowDevToolAccess"])
        self.assertEqual(sandbox["auth"], {"gh": False, "git": False})
        self.assertEqual(
            sandbox["userPolicy"]["filesystem"]["readwritePaths"],
            [],
        )
        self.assertEqual(
            sandbox["userPolicy"]["filesystem"]["readonlyPaths"],
            [str(self.root.resolve())],
        )
        self.assertFalse(sandbox["userPolicy"]["network"]["allowOutbound"])
        self.assertFalse(sandbox["userPolicy"]["network"]["allowLocalNetwork"])

    def test_copilot_adapter_rejects_modified_presets_before_launch(self) -> None:
        executable = self.make_fake_provider("github-copilot")
        reviewed = build_preset(
            "github-copilot",
            agent_id="copilot-unsafe",
            executable=str(executable),
        )
        unsafe = replace(reviewed, command=reviewed.command + ("--yolo",))

        with self.assertRaises(ValidationError):
            CopilotCliAdapter().validate_execution(unsafe, "inspect", self.root)

    def test_copilot_adapter_does_not_follow_auth_state_symlinks(self) -> None:
        source_home = self.root / "copilot-symlink-source"
        source_home.mkdir()
        external = self.root / "external-config.json"
        external.write_text('{"token": "must-not-copy"}', encoding="utf-8")
        (source_home / "config.json").symlink_to(external)
        executable = self.make_fake_provider("github-copilot")
        spec = build_preset(
            "github-copilot",
            executable=str(executable),
            config_home=str(source_home),
        )

        result = CopilotCliAdapter().execute(spec, "inspect", self.root)

        self.assertEqual(result.status, "completed")
        self.assertFalse(json.loads(result.stdout)["auth_state_present"])

    def test_codex_presets_disable_surfaces_outside_command_sandbox(self) -> None:
        for preset_id in (
            "codex-cli",
            "codex-cli-write",
            "codex-app-server",
            "codex-app-server-write",
        ):
            with self.subTest(preset_id=preset_id):
                arguments = PRESETS[preset_id].fixed_arguments
                self.assertIn('web_search="disabled"', arguments)
                self.assertIn("mcp_servers={}", arguments)
                disabled_features = {
                    arguments[index + 1]
                    for index, argument in enumerate(arguments[:-1])
                    if argument == "--disable"
                }
                self.assertTrue(
                    {
                        "apps",
                        "plugins",
                        "remote_plugin",
                        "hooks",
                        "browser_use",
                        "computer_use",
                        "in_app_browser",
                    }.issubset(disabled_features)
                )

        for preset_id in ("codex-cli", "codex-cli-write"):
            with self.subTest(preset_id=preset_id):
                arguments = PRESETS[preset_id].fixed_arguments
                self.assertIn("--ignore-user-config", arguments)
                self.assertIn("--ignore-rules", arguments)

        read_arguments = PRESETS["codex-cli"].fixed_arguments
        self.assertIn('default_permissions="agent-relay-read"', read_arguments)
        self.assertIn(
            CODEX_EXPECTED_READ_PERMISSION_ARGUMENTS[-1],
            read_arguments,
        )
        write_arguments = PRESETS["codex-cli-write"].fixed_arguments
        self.assertIn('default_permissions="agent-relay-write"', write_arguments)
        self.assertIn(
            CODEX_EXPECTED_WRITE_PERMISSION_ARGUMENTS[-1],
            write_arguments,
        )
        self.assertNotIn("--sandbox", read_arguments)
        self.assertNotIn("--sandbox", write_arguments)

    def test_claude_write_preset_is_restricted_to_repository_file_tools(self) -> None:
        arguments = PRESETS["claude-code-write"].fixed_arguments
        executable = self.make_fake_provider("claude-code-write")
        config_home = self.root / "claude-write-config"
        spec = build_preset(
            "claude-code-write",
            executable=str(executable),
            config_home=str(config_home),
        )

        self.assertIn("--safe-mode", arguments)
        self.assertIn("--restricted", arguments)
        self.assertEqual(
            arguments[arguments.index("--permission-mode") + 1],
            "acceptEdits",
        )
        self.assertEqual(
            arguments[arguments.index("--tools") + 1],
            "Read,Edit,Write,Glob,Grep",
        )
        self.assertEqual(
            arguments[arguments.index("--disallowedTools") + 1],
            "mcp__*",
        )
        self.assertNotIn("Bash", arguments[arguments.index("--tools") + 1])
        self.assertEqual(
            spec.config_home,
            ("CLAUDE_CONFIG_DIR", str(config_home.resolve())),
        )

    def test_claude_read_preset_is_restricted_to_repository_read_tools(self) -> None:
        preset = PRESETS["claude-code"]
        arguments = preset.fixed_arguments

        self.assertIn("--safe-mode", arguments)
        self.assertIn("--restricted", arguments)
        self.assertEqual(
            arguments[arguments.index("--permission-mode") + 1],
            "plan",
        )
        self.assertEqual(
            arguments[arguments.index("--tools") + 1],
            "Read,Glob,Grep",
        )
        self.assertEqual(
            arguments[arguments.index("--disallowedTools") + 1],
            "mcp__*",
        )
        self.assertIn("--no-chrome", arguments)
        self.assertIn("--disable-slash-commands", arguments)
        self.assertIn("--no-session-persistence", arguments)
        self.assertEqual(preset.minimum_version, "2.1.248")

    def test_automatic_route_presets_remain_read_only(self) -> None:
        for preset_id in (
            "claude-code",
            "codex-cli",
            "codex-app-server",
            "github-copilot",
        ):
            with self.subTest(preset_id=preset_id):
                self.assertEqual(PRESETS[preset_id].capabilities, ("repo-read",))
                self.assertNotEqual(
                    PRESETS[preset_id].permission_profile,
                    "workspace-write",
                )

    def test_uncontained_plan_presets_are_labeled_manual_only(self) -> None:
        for preset_id in ("antigravity-cli", "gemini-cli"):
            with self.subTest(preset_id=preset_id):
                self.assertEqual(
                    PRESETS[preset_id].permission_profile,
                    "manual-plan-uncontained",
                )
                self.assertEqual(PRESETS[preset_id].capabilities, ("repo-read",))

    def test_provider_shaped_handoffs_complete_through_registry(self) -> None:
        self.service.register_agent(
            AgentSpec(
                agent_id="source",
                display_name="Source",
                command=(sys.executable, "-c", "print('source')"),
            )
        )

        excluded = {
            preset_id
            for preset_id, preset in PRESETS.items()
            if preset.adapter_type == "codex-app-server"
            or "repo-write" in preset.capabilities
        }
        for preset_id in set(EXPECTED_ARGUMENTS).difference(excluded):
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
        self.assertEqual(loaded.permission_profile, "read-only")

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
        self.assertEqual(registered.provider_id, "codex-cli")
        self.assertEqual(
            registered.config_home,
            ("CODEX_HOME", str(config_home.resolve())),
        )


if __name__ == "__main__":
    unittest.main()
