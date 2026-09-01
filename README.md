# Agent Relay

[![CI](https://github.com/Oussamoux1234/agent-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/Oussamoux1234/agent-relay/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Agent Relay is a local-first continuity layer for AI coding agents. It saves an explicit task checkpoint and hands that checkpoint to another user-owned runtime, such as a CLI agent, local script, or future API adapter.

The MVP answers one question: **can a user register arbitrary agents and move a task between them without losing verified work state or blindly repeating uncertain actions?**

## What works now

- Register any local executable as an agent adapter.
- Register reviewed presets for Codex CLI, Claude Code, Gemini CLI, and Antigravity CLI.
- Describe its fixed arguments, prompt transport, capabilities, timeout, and permitted environment-variable names.
- Create a versioned, inspectable task checkpoint.
- Record summaries, decisions, constraints, changed files, tests, and next steps.
- Preview the exact prompt before a handoff.
- Execute a handoff without invoking a shell.
- Record every handoff as pending before the target process starts.
- Mark manual-handoff timeouts and non-zero exits as `unknown` and block further handoffs until the user resolves the outcome.
- Configure an ordered route across Codex, Claude, Gemini, and Antigravity preset instances.
- Continue to the next read-only agent after a documented quota/rate-limit signal or a process that could not start.
- Fail closed on authentication, timeout, overload, and unrecognized routed failures.
- Extract bounded structured-result proposals from successful routed agents.
- Preview proposed memory changes and accept them only with an unchanged checkpoint revision.
- Persist local state atomically with owner-only permissions where the filesystem supports them.

Agent Relay does not claim to transfer a model's hidden reasoning or private session. It transfers an auditable set of facts about the task.

## Architecture

```text
                                  ┌─ Codex CLI adapter
User ──> Relay service contract ──┼─ Claude Code adapter
              │                   ├─ Gemini CLI adapter
              │                   ├─ Antigravity adapter
              │                   └─ custom command adapter
              │
              ├─ versioned checkpoint
              ├─ ordered fallback policy
              ├─ structured-result proposal
              └─ action ledger + redacted failure class/digest
```

`AgentSpec` identifies the runtime type, and `AdapterRegistry` resolves it to an implementation. `CliAgentAdapter` launches a configured argv list with `shell=False`. Future API and application-extension adapters can be added without changing checkpoint persistence or handoff policy.

## Run the harmless demo

No installation or third-party dependency is required:

```bash
python3 -m agent_relay --state-dir .demo-relay agent add codex \
  --name "Codex" \
  --command python3 \
  --arg examples/demo_agent.py

python3 -m agent_relay --state-dir .demo-relay agent add gemini-cli \
  --name "Gemini CLI" \
  --capability repo \
  --transport stdin \
  --command python3 \
  --arg examples/demo_agent.py

python3 -m agent_relay --state-dir .demo-relay task create \
  --title "Continuity demo" \
  --goal "Continue this task with another agent" \
  --agent codex
```

Copy the returned `task_id`, then preview the checkpoint:

```bash
python3 -m agent_relay --state-dir .demo-relay handoff TASK_ID gemini-cli
```

Execute the handoff only after inspecting the preview:

```bash
python3 -m agent_relay --state-dir .demo-relay handoff TASK_ID gemini-cli \
  --execute \
  --cwd .
```

## Register a real or custom CLI

Supply the executable separately from each fixed argument. This keeps untrusted checkpoint text out of a shell command:

```bash
python3 -m agent_relay agent add my-agent \
  --name "My Agent" \
  --transport stdin \
  --timeout 900 \
  --capability repo \
  --capability shell \
  --allow-env MY_PROVIDER_API_KEY \
  --command my-agent \
  --arg=--non-interactive
```

The example flag is illustrative; use the non-interactive invocation supported by the locally installed agent. Environment values are never stored. Only base process variables and explicitly allowed names are passed to the child.

Adapters still run with the user's operating-system permissions and can access the selected working directory. Relay coordinates agents; it is not an operating-system sandbox. Captured stdout and stderr are bounded and returned to the invoking user, but are not added to the checkpoint ledger.

## Built-in provider presets

Check which supported CLIs are installed without launching a model request:

```bash
python3 -m agent_relay agent presets
```

The current presets are deliberately analysis-only:

| Preset | Executable | Restricted mode |
| --- | --- | --- |
| `codex-cli` | `codex` | ephemeral `exec` with a read-only sandbox |
| `claude-code` | `claude` | non-interactive plan mode |
| `gemini-cli` | `gemini` | headless plan approval mode |
| `antigravity-cli` | `agy` | plan mode with JSON-lines stdin |
| `github-copilot` | `copilot` | read tools only; parked for the later GitHub Education phase |

Install and authenticate a provider through its own official workflow, then register only the presets you use:

```bash
python3 -m agent_relay agent add-preset codex-cli
python3 -m agent_relay agent add-preset claude-code
python3 -m agent_relay agent add-preset gemini-cli
python3 -m agent_relay agent add-preset antigravity-cli
```

Every active preset follows the same baseline:

- The portable checkpoint travels over stdin and is never interpolated into a shell command.
- The provider is placed in its documented read-only or planning mode.
- Relay reuses the CLI's local authentication and never stores provider tokens.
- Dangerous auto-approval and permission-bypass flags are not enabled.

This first profile proves memory continuity without authorizing file changes. A workspace-write profile should be added later as a separate, explicit policy instead of silently broadening these presets. Provider behavior is based on the official [Codex non-interactive guide](https://developers.openai.com/codex/noninteractive), [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage), [Gemini CLI headless guide](https://geminicli.com/docs/cli/headless/), and [Antigravity headless guide](https://antigravity.google/docs/cli/headless/).

### Codex App support

Relay can invoke the locally installed Codex CLI as a separate headless process. It does not yet control a Codex desktop task or extract that task's private chat history. The explicit checkpoint is the shared memory layer between the app, CLI agents, and future API adapters.

GitHub Copilot remains available as a parked preset for the later GitHub Education phase; it is not part of the current connector rollout.

### Multiple Codex or Claude instances

Agent IDs identify instances, not provider brands. Register the same preset more than once to switch between two Codex accounts/configurations or two Claude accounts/configurations:

```bash
# Authenticate each isolated provider home using the provider's own CLI first.
CODEX_HOME="$HOME/.codex-primary" codex login
CODEX_HOME="$HOME/.codex-backup" codex login
CLAUDE_CONFIG_DIR="$HOME/.claude-primary" claude auth login
CLAUDE_CONFIG_DIR="$HOME/.claude-backup" claude auth login

# Relay persists only the absolute directory paths, never the credentials inside them.
python3 -m agent_relay agent add-preset codex-cli \
  --id codex-primary --config-home "$HOME/.codex-primary"
python3 -m agent_relay agent add-preset codex-cli \
  --id codex-backup --config-home "$HOME/.codex-backup"
python3 -m agent_relay agent add-preset claude-code \
  --id claude-primary --config-home "$HOME/.claude-primary"
python3 -m agent_relay agent add-preset claude-code \
  --id claude-backup --config-home "$HOME/.claude-backup"
```

The active task can then hand off or route from `codex-primary` to `codex-backup`, or from `claude-primary` to `claude-backup`, because each registration has a unique agent ID. For file-based Codex account isolation, configure `cli_auth_credentials_store = "file"` inside each Codex home; never commit either provider directory.

## Safe ordered fallback routing

Set an explicit priority order. The first agent must be the task's current active agent, and every entry must come from a supported built-in read-only preset:

```bash
python3 -m agent_relay route set TASK_ID \
  --agent codex-primary \
  --agent codex-backup \
  --agent claude-primary \
  --agent gemini-cli

python3 -m agent_relay route show TASK_ID
```

Preview the candidate order and exact checkpoint prompt without launching an agent:

```bash
python3 -m agent_relay route run TASK_ID
```

Execute the route after checking the preview:

```bash
python3 -m agent_relay route run TASK_ID --execute --cwd .
```

Relay invokes the active CLI agent first. If that process exits with a recognized quota or rate-limit signal, Relay records a redacted failure class and immediately invokes the next candidate with the shared checkpoint. A process that cannot be launched is also skipped safely because it could not have performed work. A successful candidate becomes the task's active agent; later runs start there and continue only through the remaining route entries.

The detector recognizes documented signals such as OpenAI 429/quota codes, Claude `rate_limit_error`, and Gemini `429 RESOURCE_EXHAUSTED`. Authentication or payment errors, timeouts, overloads, and unrecognized non-zero exits block the task instead of launching another process. These rules follow the official [OpenAI error-code guide](https://developers.openai.com/api/docs/guides/error-codes), [Claude API error reference](https://platform.claude.com/docs/en/api/errors), and [Gemini troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting).

Failure attempts persist only classifications and execution metadata. Successful attempts may persist a bounded, validated result proposal for review. Raw stdout and stderr are returned to the invoking user but are not written into the task ledger. Automatic routing currently applies to Relay-launched CLI processes. It cannot observe a limit encountered inside an unrelated Codex App, Claude Code, or Antigravity session.

## Review and accept fresh agent memory

Every executed route now asks the successful agent to finish with a marked JSON result envelope containing only these fields:

- `summary`
- `decisions`
- `constraints`
- `files_changed`
- `tests`
- `next_steps`

The envelope is tied to the exact `task_id` and `source_action_id`. Relay understands plain output and the JSON response wrappers used by Codex, Claude, Gemini, and Antigravity. A valid proposal appears in the `route run --execute` response with `result_status: "pending"` and an `action_id`.

Preview the exact state change without mutating the checkpoint:

```bash
python3 -m agent_relay result preview TASK_ID ACTION_ID
```

The preview returns `checkpoint_revision`, the proposed summary, and only the new list entries after de-duplication. Accept it only after review:

```bash
python3 -m agent_relay result accept TASK_ID ACTION_ID \
  --expected-revision CHECKPOINT_REVISION
```

Acceptance fails if the checkpoint changed after preview, an unresolved action exists, or a later agent execution made the proposal stale. Missing markers, malformed JSON, unknown fields, wrong task/action IDs, ambiguous envelopes, and oversized content never modify task memory. Pending proposals are redacted from later agent prompts; after acceptance, the proposal is removed from the action details and replaced by a SHA-256 digest plus field counts.

This is explicit, reviewable memory—not independent verification of an agent's claims. Relay does not automatically run reported tests or trust reported files. Provider response handling follows the official [Claude headless output](https://code.claude.com/docs/en/headless), [Gemini automation](https://geminicli.com/docs/cli/tutorials/automation/), and [Antigravity streaming JSON](https://antigravity.google/docs/cli/headless/) formats.

## Update the shared checkpoint

```bash
python3 -m agent_relay --state-dir .demo-relay task note TASK_ID \
  --summary "Authentication endpoint implemented" \
  --decision "Use signed cookies" \
  --file "app/auth.py" \
  --test "python3 -m unittest: passed" \
  --next "Add logout"
```

If a target process times out or exits non-zero, inspect the workspace and then resolve the ledger entry explicitly:

```bash
python3 -m agent_relay --state-dir .demo-relay resolve TASK_ID ACTION_ID --as completed
# or: --as failed / --as cancelled
```

## Run tests

```bash
PYTHONPYCACHEPREFIX=/tmp/agent-relay-pycache python3 -m unittest discover -v
```

The GitHub Actions workflow runs the same suite on Python 3.9 through 3.14 with read-only repository permissions.

## Deliberate MVP boundaries

- Manual handoffs remain user-confirmed; ordered CLI routes can automatically continue only after conservative limit classification.
- The adapter launches CLI processes but does not scrape or impersonate consumer subscriptions.
- All built-in provider presets are read-only or plan-only; a workspace-write profile is not enabled yet.
- Codex App task control is not implemented; the current Codex integration is CLI-based.
- Structured result fields are agent-reported and user-approved; automatic filesystem diff and test verification is not implemented yet.
- Provider cooldown windows and automatic return to an earlier route entry are not implemented yet.
- State is local JSON and optimized for one writer. A service deployment will need transactional storage and stronger concurrency control.
- There is no HTTP API or graphical interface yet.

Work is tracked in [GitHub Issues](https://github.com/Oussamoux1234/agent-relay/issues), with one scoped issue per milestone.

## License

Agent Relay is licensed under the [Apache License 2.0](LICENSE).

## Next milestone

[Track provider cooldowns and route recovery](https://github.com/Oussamoux1234/agent-relay/issues/5) so recently limited agents are skipped until their documented retry window expires. Codex App integration and explicitly approved workspace-write profiles remain separate later milestones.
