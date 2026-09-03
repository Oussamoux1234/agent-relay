# Agent Relay

[![CI](https://github.com/Oussamoux1234/agent-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/Oussamoux1234/agent-relay/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Agent Relay is a local-first continuity layer for AI coding agents. It saves an explicit task checkpoint and hands that checkpoint to another user-owned runtime, such as a CLI agent, local script, or Codex App Server.

The MVP answers one question: **can a user register arbitrary agents and move a task between them without losing verified work state or blindly repeating uncertain actions?**

## What works now

- Register any local executable as an agent adapter.
- Register reviewed presets for Codex CLI, Codex App Server, Claude Code, Gemini CLI, Antigravity CLI, and GitHub Copilot CLI.
- Describe its fixed arguments, prompt transport, capabilities, timeout, and permitted environment-variable names.
- Create a versioned, inspectable task checkpoint.
- Record summaries, decisions, constraints, changed files, tests, and next steps.
- Preview the exact prompt before a handoff.
- Execute a handoff without invoking a shell.
- Start or resume a Codex thread through the documented local App Server stdio protocol.
- Return a Codex App answer while retaining only safe thread/turn metadata and an explicit result proposal.
- Record every handoff as pending before the target process starts.
- Mark manual-handoff timeouts and non-zero exits as `unknown` and block further handoffs until the user resolves the outcome.
- Configure an ordered automatic route across currently eligible Codex, Claude, and GitHub Copilot preset instances.
- Continue to the next read-only agent after a documented quota/rate-limit signal or a process that could not start.
- Persist redacted health per agent instance and skip entries whose cooldown is still active.
- Inspect or clear cooldowns and explicitly recover a task to an earlier route entry.
- Fail closed on authentication, timeout, overload, and unrecognized routed failures.
- Extract bounded structured-result proposals from successful routed agents.
- Preview proposed memory changes and accept them only with an unchanged checkpoint revision.
- Opt a reviewed Codex CLI, Codex App Server, or Claude Code write preset into one exact task and Git root.
- Restrict reviewed Codex reads to the active workspace plus Codex's platform-minimal runtime paths.
- Disable Codex web search, MCP, apps/plugins, hooks, browser/computer use, and other external tool surfaces independently of command-network isolation.
- Snapshot Git state before and after a write, then require explicit change acceptance or verified rollback.
- Keep every write-capable agent out of automatic fallback routes.
- Persist local state atomically with owner-only permissions where the filesystem supports them, without following managed-path symlinks or losing concurrent updates.

Agent Relay does not claim to transfer a model's hidden reasoning or private session. It transfers an auditable set of facts about the task.

## Architecture

```text
                                  ┌─ Codex CLI adapter
User ──> Relay service contract ──┼─ Claude Code adapter
              │                   ├─ Gemini CLI adapter
              │                   ├─ Antigravity adapter
              │                   ├─ GitHub Copilot CLI adapter
              │                   ├─ Codex App Server adapter
              │                   └─ custom command adapter
              │
              ├─ versioned checkpoint
              ├─ ordered fallback policy
              ├─ per-instance health + cooldown registry
              ├─ structured-result proposal
              ├─ task/agent/root write authorization
              ├─ bounded Git snapshots + review gate
              └─ action ledger + redacted failure class/digest
```

`AgentSpec` identifies the runtime type, and `AdapterRegistry` resolves it to an implementation. `CliAgentAdapter` launches a configured argv list with `shell=False`; `CodexAppServerAdapter` drives one bounded JSONL turn over stdio. Future API and application-extension adapters can be added without changing checkpoint persistence or handoff policy.

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

## Installation runbooks

- [macOS native setup and operations](docs/runbook-macos.md)
- [Windows setup and operations through WSL2](docs/runbook-windows.md)

Native Windows Python is not currently supported because Relay's hardened state store
depends on POSIX advisory locks and descriptor-relative filesystem operations. The
Windows runbook uses WSL2 so those safety guarantees remain intact.

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

Custom adapters still run with the user's operating-system permissions and can access the selected working directory. Relay coordinates agents; it is not itself an operating-system sandbox. Reviewed write presets also request the provider's bounded native permission mode. Captured stdout and stderr are bounded and returned to the invoking user, but are not added to the checkpoint ledger.

Relay stores state outside the current repository by default: `${XDG_STATE_HOME:-$HOME/.local/state}/agent-relay` on Linux/WSL and `~/Library/Application Support/agent-relay` on macOS. `AGENT_RELAY_STATE_DIR` or `--state-dir` can select an existing safe location. A state root used with workspace-write must be disjoint from the authorized Git root: it cannot equal, contain, or be contained by that workspace. Relay checks this when authorization is created and again before execution, so an unsafe legacy authorization fails closed.

The default-location change does not move or delete an older state directory. Existing users can continue with `--state-dir /absolute/safe/state` or `AGENT_RELAY_STATE_DIR=/absolute/safe/state`. If the old state is inside a repository that will receive workspace-write access, first stop Relay activity and move that complete state directory to a location outside the repository; do not split or manually edit its managed files.

The state store canonicalizes parent components but requires the selected state root itself, its managed `tasks` directory, and every managed JSON file to be real directories or regular files rather than symlinks. It keeps directory identities for the lifetime of the process and performs reads, temporary-file creation, replacement, and cleanup relative to verified directory descriptors. If a managed directory is replaced or the platform cannot provide the required safe path operations, Relay fails closed instead of following the new path.

Every task, agent-registry, and health-registry mutation holds an exclusive operating-system lock on the owner-only `.relay.lock` file for its complete read–modify–write transaction. Separate Relay CLI processes on the same local filesystem therefore cannot both accept the same task revision or overwrite independent registry changes. Read-only commands remain lock-free because state files are replaced atomically. The lock is advisory—other programs must not edit Relay state directly—and is released automatically if a process exits. Atomic replacement prevents a partially written JSON file from becoming current; an interrupted pre-replacement temporary file may remain but is ignored. Existing state directories create the lock file on first use without a schema migration.

## Built-in provider presets

Check which supported CLIs are installed without launching a model request:

```bash
python3 -m agent_relay agent presets
```

Read-only planning remains the default. Three separately named presets expose the opt-in write policy without changing their read counterparts:

| Preset | Executable | Execution policy |
| --- | --- | --- |
| `codex-cli` | `codex` | ephemeral `exec`; permission profile reads only platform-minimal paths and the active workspace |
| `codex-app-server` | `codex` | one App Server turn; restricted workspace reads and no approvals |
| `codex-cli-write` | `codex` | ephemeral `exec`; permission profile writes only the active workspace, with on-request escalation |
| `codex-app-server-write` | `codex` | one App Server turn; one writable/readable workspace root, no command network or temporary-directory writes |
| `claude-code` | `claude` | restricted, safe, non-persistent plan mode with repository read tools only; requires Claude Code 2.1.248+ |
| `claude-code-write` | `claude` | restricted mode with repository file tools only; requires Claude Code 2.1.248+ |
| `gemini-cli` | `gemini` | explicit handoff only; headless Plan Mode is not a read-only boundary |
| `antigravity-cli` | `agy` | explicit handoff only; Plan Mode is not a read-only boundary |
| `github-copilot` | `copilot` | isolated config plus fail-closed local sandbox; contained repository reads only; requires Copilot CLI 1.0.79+ |

Install and authenticate a provider through its own official workflow, then register only the presets you use:

```bash
python3 -m agent_relay agent add-preset codex-cli
python3 -m agent_relay agent add-preset codex-app-server
python3 -m agent_relay agent add-preset claude-code
python3 -m agent_relay agent add-preset gemini-cli
python3 -m agent_relay agent add-preset antigravity-cli
python3 -m agent_relay agent add-preset github-copilot
```

Every preset follows the same baseline:

- The portable checkpoint travels over stdin and is never interpolated into a shell command.
- Automatic-route presets use reviewed provider controls; Gemini and Antigravity
  plan presets remain manual-only because their headless modes do not enforce a
  repository read-only boundary.
- Write access requires a separately registered built-in write preset plus a task-specific authorization.
- Relay reuses the CLI's local authentication and never stores provider tokens.
- Dangerous auto-approval and permission-bypass flags are not enabled.

Existing read registrations are never upgraded to write access. Codex CLI presets use OpenAI's documented [permission profiles](https://learn.chatgpt.com/docs/permissions): filesystem access is denied by default, `:minimal` runtime paths are readable, the active workspace root is read-only or writable according to the preset, and command network access is disabled. Codex App Server turns use its documented [restricted read-access policy](https://learn.chatgpt.com/docs/app-server) with the canonical workspace as the only added readable root. Both Claude presets require Claude Code 2.1.248 or newer and use Anthropic's documented [`--restricted` and `--safe-mode` controls](https://code.claude.com/docs/en/cli-usage). Other provider behavior is based on the official [Codex non-interactive guide](https://developers.openai.com/codex/noninteractive), [Gemini CLI headless guide](https://geminicli.com/docs/cli/headless/), [Antigravity headless guide](https://antigravity.google/docs/cli/headless/), and [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference).

The Claude read preset permits only `Read`, `Glob`, and `Grep`, denies every MCP tool, disables Chrome integration, slash commands, and session persistence, and starts in Plan Mode. Restricted mode confines built-in file tools to the working directory; safe mode prevents user and project CLAUDE.md files, skills, plugins, hooks, MCP servers, commands, agents, workflows, and auto memory from loading. Managed policy settings still apply, and Anthropic explicitly notes that policy-configured hooks may still execute. Treat those organization-controlled hooks as trusted administrator code or run Relay and Claude inside an outer OS sandbox; Relay cannot disable them from the command line.

Command network isolation is not a global offline switch. OpenAI documents that web search, apps/connectors, MCP servers, browser/computer use, Codex service traffic, and Codex Cloud have [separate external-surface controls](https://learn.chatgpt.com/docs/agent-approvals-security). Every reviewed Codex preset therefore also disables web search, configured MCP servers, apps/plugins, hooks, browser/computer use, image generation, remote plugins, MCP dependency installation, and tool suggestions through explicit command-line overrides. Model and authentication traffic still reaches the Codex service because the provider cannot run without it; Relay does not enable or launch Codex Cloud tasks.

### Codex App support

Relay has a prototype connector for OpenAI's documented [Codex App Server](https://developers.openai.com/codex/app-server) interface. The connector launches the local `codex app-server` process, performs the required `initialize` handshake, starts or resumes a thread, sends one explicit checkpoint in `turn/start`, and consumes item and turn completion notifications. It uses the documented stdio JSONL transport and stable protocol methods; it does not enable experimental WebSocket transport or capabilities. The Codex CLI currently still labels App Server tooling experimental, so keep the installed CLI current and treat this connector as a version-sensitive prototype.

Register the connector after installing and authenticating Codex through OpenAI's workflow:

```bash
python3 -m agent_relay agent add-preset codex-app-server --id codex-app
```

Preview the checkpoint normally, then explicitly authorize one read-only turn:

```bash
python3 -m agent_relay handoff TASK_ID codex-app
python3 -m agent_relay handoff TASK_ID codex-app --execute --cwd .
```

The first execution starts a thread. Relay returns its ID and records it as `external_session_id`; after the task moves to another agent, a later handoff back to the same registered app agent resumes that thread automatically. To attach a checkpoint to a known Codex thread on the first handoff, identify it explicitly:

```bash
python3 -m agent_relay handoff TASK_ID codex-app \
  --execute --cwd . --thread-id CODEX_THREAD_ID
```

The current safety boundary is intentional:

- `--execute` is the user's authorization for exactly one app-server turn.
- The turn uses `approvalPolicy: never`, disabled command network access, and a restricted read policy whose only added readable root is the canonical workspace. Codex's platform-minimal defaults remain enabled so normal developer tools can start.
- If Codex requests a command or file-change approval anyway, Relay declines it. Other unsupported server requests fail closed.
- Relay returns only completed `agentMessage` text. It ignores reasoning items and never imports private chat history, credentials, or hidden chain-of-thought.
- The ledger stores the thread ID, turn ID, protocol status, event method names, timing, and a bounded structured checkpoint proposal—not the raw answer or event payloads.
- Timeouts, malformed protocol messages, and failed/interrupted turns become `unknown` actions and block another handoff until the user resolves them.

This is a supported protocol integration, not desktop UI automation. Relay does not click or scrape the Codex app, discover private tasks, attach to a currently running UI process, or impersonate a browser session. It starts a local App Server process and can resume only a thread ID available to that configured Codex installation. App Server handoffs are manual and are not included in automatic quota routing yet.

### GitHub Copilot and GitHub Education

The `github-copilot` preset is an active, contained repository-read fallback for
GitHub Copilot CLI 1.0.79 or newer. A dedicated adapter creates a fresh private
configuration and cache for every run, copies only bounded authentication state,
and exposes only Copilot's `view`, `glob`, and `grep` tools. It denies every write,
shell, PowerShell, URL, memory, subagent, and web-fetch surface; disables built-in
MCP, custom instructions, remote sessions, remote export, and sandbox bypass; and
requires Copilot's OS-backed local sandbox to start successfully. The sandbox
grants the canonical repository read-only, grants no writable path, withholds
Git/`gh`/Keychain credentials from tools, and blocks child-process network access.

Install and authenticate the official CLI first, then register it:

```bash
copilot login
python3 -m agent_relay agent add-preset github-copilot --id copilot-read
```

Relay reuses Copilot CLI's local OAuth login or GitHub CLI fallback. It does not
store, mint, refresh, or choose GitHub tokens. Authentication or organization-policy
errors fail closed. The preset recognizes GitHub's documented rate-limit wording as
transient and exhausted AI credits as quota exhaustion; neither classification
changes, extends, or bypasses the user's GitHub plan.

Relay disables repository, user, and plugin hooks for these programmatic runs and
isolates saved folder trust. GitHub's administrator-installed policy hooks are an
explicit trusted-computing-base exception: GitHub says they are machine-wide and
cannot be disabled by `disableAllHooks`. Local sandboxing is also a public-preview
feature. Read the exact threat model and run the authenticated host fixture in
[the Copilot containment guide](docs/copilot-containment.md) before calling a
specific host/version combination hard read-only.

GitHub Education is an entitlement managed entirely by GitHub, not a Relay feature.
Verified students can activate Copilot Student from their
[Education benefits page](https://github.com/settings/education/benefits); GitHub
documents that Education approval and Copilot activation are separate and that
eligibility is reevaluated monthly. Relay works the same with Student, Free, paid,
and organization-provided access when the installed CLI and account policy permit
it. See GitHub's [student setup guide](https://docs.github.com/en/copilot/how-tos/copilot-on-github/set-up-copilot/enable-copilot/set-up-for-students),
[CLI installation guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli),
and [authentication guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli).

### Explicit workspace-write flow

Workspace writes use two independent keys: the agent registration must come from a reviewed write preset, and the task ledger must authorize that exact agent ID for one canonical Git root. Register a write agent without changing any existing read agent:

```bash
python3 -m agent_relay agent add-preset codex-cli-write --id codex-writer
# App Server is also supported:
python3 -m agent_relay agent add-preset codex-app-server-write --id codex-app-writer
# Claude Code 2.1.248+ is also supported:
python3 -m agent_relay agent add-preset claude-code-write --id claude-writer
```

Authorize only the agent and repository needed for the task:

```bash
python3 -m agent_relay workspace authorize TASK_ID codex-writer \
  --root /absolute/path/to/repository
```

Authorization fails unless the path is the exact top level of an existing Git repository and Relay's state directory is outside and disjoint from it. It is recorded as a `workspace-write-authorize` action scoped to the task, agent ID, and canonical root. It does not authorize a different task, a second provider registration, a subdirectory, or another repository.

Preview and execute the handoff normally, using that same root as the working directory:

```bash
python3 -m agent_relay handoff TASK_ID codex-writer
python3 -m agent_relay handoff TASK_ID codex-writer \
  --execute --cwd /absolute/path/to/repository
```

Relay records a bounded snapshot before launch and another after the process returns. The review contains SHA-256 state digests, HEAD and branch changes, pre-existing dirty paths, introduced/modified/removed paths, and final dirty paths. Tracked, staged, untracked, and ignored paths are covered; snapshot format v2 hashes the mode, object ID, and conflict stage of each relevant Git index entry separately from the working-tree file. Repositories containing `assume-unchanged` or `skip-worktree` entries fail closed because those flags can hide edits from Git status. File contents, staged blobs, and raw diffs are never copied into Relay state. Inspection is limited to 2,000 relevant paths, 128 MiB per file, 512 MiB total, and 2 MiB per Git command output so an unbounded repository fails before execution or blocks safely after execution.

Workspace reviews written before v0.7.1 remain readable. Relay can verify an old snapshot only when its current state has no staged paths; legacy reviews involving staged content fail closed and should be resolved with v0.7.0 before upgrading. New reviews record their snapshot version explicitly.

If a successful run changed the snapshot, the task stays blocked. Ask Relay for the review metadata, then inspect the actual repository diff with Git:

```bash
python3 -m agent_relay workspace review TASK_ID WRITE_ACTION_ID
git status --short
git diff
git diff --cached
```

Accept only the exact snapshot you inspected. Both the checkpoint revision and current workspace digest must still match:

```bash
python3 -m agent_relay workspace accept TASK_ID WRITE_ACTION_ID \
  --expected-revision CHECKPOINT_REVISION \
  --cwd /absolute/path/to/repository
```

If the run timed out, exited non-zero, lost protocol state, or could not be inspected afterward, its effects are ambiguous and the action always stays blocked. `resolve` cannot override a write-capable action. Restore only this action's changes without discarding pre-existing work, then let Relay verify that the complete pre-run snapshot is back:

An interrupted Relay command terminates and reaps the provider process group before the interruption escapes. If the provider may have started, Relay first records the action as `unknown`; workspace-write actions also receive a post-run snapshot or an explicit unavailable review. Automatic routes never continue to a backup after an interruption.

```bash
python3 -m agent_relay workspace verify-rollback TASK_ID WRITE_ACTION_ID \
  --expected-revision CHECKPOINT_REVISION \
  --cwd /absolute/path/to/repository
```

Relay supplies rollback guidance but never deletes files, resets Git history, or performs rollback itself. You can revoke future use of the grant after all write reviews are resolved:

```bash
python3 -m agent_relay workspace revoke TASK_ID codex-writer
```

The Codex CLI write preset ignores user configuration and execution rules, selects a dedicated permission profile with `on-request` escalation, denies full-host reads and both temporary-directory roots, allows writes only inside the active workspace, and disables command network access. The App Server write preset sends the same one-root, restricted-read, no-command-network, no-temp boundary in every turn. Relay declines App Server requests that cross the approved boundary.

The Claude write preset requires Claude Code 2.1.248 or newer and combines `--safe-mode`, `--restricted`, and `acceptEdits`. Its built-in tool set is limited to `Read`, `Edit`, `Write`, `Glob`, and `Grep`; Bash, web tools, slash commands, session persistence, MCP tools, and local customizations are excluded. Restricted mode ignores user and project settings and confines file tools to the working directory. An older CLI or any rejected flag produces a non-zero result that Relay treats as ambiguous and blocks for review.

Gemini [`auto_edit`](https://geminicli.com/docs/reference/configuration/#approval-mode) and Antigravity [`accept-edits`](https://antigravity.google/docs/cli/modes/) were evaluated but do not yet have write presets. Their current [Gemini sandbox](https://geminicli.com/docs/cli/sandbox/) and [Antigravity permission](https://antigravity.google/docs/cli/permissions/) controls can still be widened by local or project settings and do not give Relay a verified, settings-independent exact-root boundary on every supported platform. Their plan presets may be used only through an explicit handoff; `route set`, route previews, and execution of stored routes reject them before launching a provider. Relay also rejects custom registrations that claim the unapproved `gemini-cli-write` or `antigravity-cli-write` provider IDs.

All write presets are rejected by `route set`, so quota fallback remains read-only and an uncertain write can never trigger another agent automatically.

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
  --agent copilot-read

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

Relay invokes the active CLI agent first. If that process exits normally with a
recognized provider-owned quota or rate-limit signal, Relay records a redacted
failure class and immediately invokes the next candidate with the shared checkpoint.
Structured terminal errors are preferred. Free-text matching is provider-specific
and reads only stderr; model output on stdout can never authorize fallback. Signal
exits, missing return codes, timeouts, and inconsistent terminal states always block.
A process that cannot be launched is skipped safely because it could not have
performed work. A successful candidate becomes the task's active agent; later runs
start there and continue only through the remaining route entries.

Automatic routes currently accept only `codex-cli`, `claude-code`, and
`github-copilot` provider registrations. Gemini and Antigravity can still be
registered and invoked by an explicit, previewed handoff, but their headless plan
modes may allow workspace changes. Run those manual handoffs only inside an
OS-enforced read-only environment until Relay ships a tested containment boundary.
Existing routes containing either provider fail closed during preview or execution
before any route candidate is launched.

Each failed instance also receives a record in the owner-only `health.json` registry. Before a route starts, Relay takes one deterministic snapshot of the remaining entries and omits every active cooldown. It visits each eligible entry at most once, never sleeps inside the route, and returns a conflict without launching anything when every remaining entry is cooling down.

Inspect the current decision state:

```bash
python3 -m agent_relay health list
python3 -m agent_relay health show codex-primary
python3 -m agent_relay route show TASK_ID
```

The cooldown policy is deliberately conservative:

- Complete JSON or JSON-lines output may supply a numeric `Retry-After` header field or a typed `google.rpc.RetryInfo.retry_delay`. If multiple valid hints exist, Relay uses the longest.
- Arbitrary prose such as `retry in 20 minutes` is never parsed as a retry window.
- A recognized transient rate limit without a structured hint gets a deterministic 60-second cooldown. An executable that could not start gets 300 seconds.
- Quota, credit, usage, or spend exhaustion without a structured retry hint stays unavailable until the user explicitly clears it. This avoids repeatedly retrying a monthly or billing-bound limit.
- Authentication, timeout, overload, and unknown failures remain fail-closed and do not create a cooldown that would hide the unresolved action.

Cooldowns belong to agent IDs, not provider brands, so `codex-primary` and `codex-backup` can recover independently. Health records contain the agent/provider IDs, redacted classification codes, timestamps, and source task/action IDs; raw provider output and credentials are never persisted there.

Clearing health is an explicit user override:

```bash
python3 -m agent_relay health clear codex-primary
```

It does not silently move a task backwards. After a successful fallback makes a later entry active, an earlier provider is considered again only after its cooldown expires or is cleared **and** the user records an explicit route recovery:

```bash
python3 -m agent_relay route recover TASK_ID codex-primary
```

Recovery changes only the active route pointer, writes an auditable `route-recover` action, and launches no provider process. The next `route run` remains preview-first unless `--execute` is supplied.

The detector recognizes documented signals such as OpenAI 429/quota codes, Claude
structured API errors plus session, weekly, model, and credit exhaustion notices,
Gemini `429 RESOURCE_EXHAUSTED`, and Copilot rate-limit or AI-credit exhaustion
messages. The cooldown fields follow the official [OpenAI error-code guide](https://developers.openai.com/api/docs/guides/error-codes),
[Claude rate-limit reference](https://platform.claude.com/docs/en/api/rate-limits),
[Google `RetryInfo` contract](https://docs.cloud.google.com/php/docs/reference/common-protos/latest/Rpc.RetryInfo),
[GitHub Copilot troubleshooting guide](https://docs.github.com/en/copilot/how-tos/troubleshoot-copilot/troubleshoot-common-issues),
and [GitHub usage-limit guide](https://docs.github.com/en/copilot/concepts/usage-limits).
General classification also follows the [Claude API error reference](https://platform.claude.com/docs/en/api/errors)
and [Gemini troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting).

Failure attempts persist only classifications and execution metadata. Successful attempts may persist a bounded, validated result proposal for review. Raw stdout and stderr are returned to the invoking user but are not written into the task ledger. Automatic routing currently applies to Relay-launched CLI processes. It cannot observe a limit encountered inside an unrelated Codex App, Claude Code, or Antigravity session.

## Review and accept fresh agent memory

Every executed route, Codex App handoff, and workspace-write action asks the successful agent to finish with a marked JSON result envelope containing only these fields:

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

Acceptance fails if the checkpoint changed after preview, an unresolved action exists, a workspace-write review is pending, or a later agent execution made the proposal stale. Missing markers, malformed JSON, unknown fields, wrong task/action IDs, ambiguous envelopes, and oversized content never modify task memory. Pending proposals are redacted from later agent prompts; after acceptance, the proposal is removed from the action details and replaced by a SHA-256 digest plus field counts.

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

If a read-only target process times out or exits non-zero, inspect the workspace and then resolve the ledger entry explicitly:

```bash
python3 -m agent_relay --state-dir .demo-relay resolve TASK_ID ACTION_ID --as completed
# or: --as failed / --as cancelled
```

Write-capable actions cannot use this manual override; use `workspace accept` after a successful reviewed change or `workspace verify-rollback` after restoring an ambiguous run.

## Run tests

```bash
PYTHONPYCACHEPREFIX=/tmp/agent-relay-pycache python3 -m unittest discover -v
```

The GitHub Actions workflow runs the same suite on Python 3.9 through 3.14 with read-only repository permissions.
When Codex CLI is installed, the native containment test invokes `codex sandbox` directly, proves that a file inside the temporary workspace is readable, and proves that a sibling file is not. It skips only when Codex is absent or the current host forbids a nested native sandbox.
The authenticated Claude native fixture is opt-in because it makes one real model request. Set `AGENT_RELAY_RUN_CLAUDE_NATIVE_TESTS=1` before running `python3 -m unittest tests.test_claude_native -v`; it plants a sibling secret plus malicious project instructions, a session hook, and an MCP server, then verifies that restricted safe mode reads only the in-workspace probe and activates none of those customizations.
The authenticated Copilot native fixture is also opt-in. Set `AGENT_RELAY_RUN_COPILOT_NATIVE_TESTS=1` before running `python3 -m unittest tests.test_copilot_native -v`; it verifies that a sibling secret is unreadable and that a hostile repository `sessionStart` hook cannot write inside or outside the workspace.

## Deliberate MVP boundaries

- Manual handoffs, including Codex App Server turns, remain user-confirmed; ordered CLI routes can automatically continue only after conservative limit classification.
- The adapter launches CLI processes but does not scrape or impersonate consumer subscriptions.
- Read-only presets remain the default; write access supports only the separately named Codex CLI, Codex App Server, and restricted Claude Code write presets. Gemini and Antigravity are manual-only because their plan modes are not hard read-only boundaries. GitHub Copilot remains eligible for contained read routing, but its local sandbox is public preview and administrator policy hooks remain trusted host code; the exact host image needs the native fixture before claiming a hard guarantee.
- Codex App integration is limited to documented local App Server stdio calls; live desktop UI control, task discovery, WebSocket transport, and automatic app routing are not implemented.
- Workspace review records content-free Git state metadata, not raw patch contents; the user must inspect the real Git diff before acceptance.
- Structured result fields and reported tests remain agent claims; Relay does not independently execute tests.
- Cooldown health is local JSON and shared across tasks that use the same agent ID; cross-machine health synchronization is not implemented yet.
- State is local JSON with same-host process locking. A multi-host service deployment will still need a transactional storage backend and distributed concurrency control.
- There is no HTTP API or graphical interface yet.

Work is tracked in [GitHub Issues](https://github.com/Oussamoux1234/agent-relay/issues), with one scoped issue per milestone.

## License

Agent Relay is licensed under the [Apache License 2.0](LICENSE).

## Roadmap

GitHub Education/Copilot read routing is delivered in v0.9.0. Version 0.9.1
removes Gemini and Antigravity from automatic routes until hard containment is
available. Version 0.9.2 moves default state outside repositories and rejects
state/workspace overlap for write-authorized tasks. Version 0.9.3 terminates provider
process groups and preserves unknown/review state across interruptions. Version 0.9.4
trusts only structured terminal errors or provider-specific stderr patterns for
automatic fallback. Version 0.9.5 restricts Codex CLI and App Server reads to the
active workspace plus platform-minimal paths and disables external tool surfaces
independently from command-network isolation. Version 0.9.6 confines Claude read
handoffs with restricted and safe modes, a read-only tool allowlist, and no local
customizations or persistence. Version 0.9.7 gives Copilot a dedicated ephemeral
adapter, disables untrusted hooks and configuration, and requires its OS-backed
repository-read/network-denied sandbox to fail closed. New provider write presets will be added only when
current official controls can preserve the same exact task/agent/root and review
boundary.
