# Agent Relay runbook: macOS

This runbook installs Agent Relay natively on macOS, connects locally authenticated
provider CLIs, and exercises the read-routing and reviewed workspace-write flows.
Agent Relay requires Python 3.9 or newer and Git.

## 1. Install Agent Relay

Open Terminal and clone the repository into a normal user-owned directory:

```bash
mkdir -p "$HOME/code"
cd "$HOME/code"
git clone https://github.com/Oussamoux1234/agent-relay.git
cd agent-relay

python3 --version
git --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

agent-relay --help
agent-relay agent presets
```

If `python3` or Git is missing, install current versions using your organization’s
approved package source before continuing. Re-run `source .venv/bin/activate` in each
new Terminal session, or call `.venv/bin/agent-relay` directly.

## 2. Install and authenticate providers

Install only the providers you intend to use, then authenticate each CLI directly.
Relay reuses the provider’s local login; it does not request or store access tokens.

For Codex CLI, follow the official [Codex CLI setup](https://learn.chatgpt.com/docs/codex/cli).
The current standalone installer is:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex
```

Choose a supported sign-in method on first launch. The same `codex` installation also
provides the local `codex app-server` command used by the App Server presets.

For the other providers, use their official installation and authentication guides:

- [Claude Code setup](https://code.claude.com/docs/en/setup)
- [Gemini CLI installation](https://geminicli.com/docs/get-started/installation/)
- [Antigravity CLI overview](https://antigravity.google/docs/cli/)

Install GitHub Copilot CLI using its official Homebrew cask, then authenticate with
GitHub's OAuth device flow:

```bash
brew install --cask copilot-cli
copilot login
```

GitHub Education eligibility is separate from Relay. Verified students must activate
Copilot Student from the
[GitHub Education benefits page](https://github.com/settings/education/benefits)
before logging in. GitHub documents the complete
[student activation flow](https://docs.github.com/en/copilot/how-tos/copilot-on-github/set-up-copilot/enable-copilot/set-up-for-students).

Confirm each installed executable from the same Terminal session:

```bash
codex --version
claude --version
gemini --version
agy --help
copilot --version
agent-relay agent presets
```

Missing providers are reported as unavailable and can simply remain unregistered.

## 3. Register agents

Register only the installed providers you intend to use. Codex CLI, Claude Code,
and GitHub Copilot read presets are eligible for automatic quota fallback routes.
Gemini and Antigravity plan presets are explicit-handoff-only because their
headless plan modes do not enforce repository read-only access:

```bash
agent-relay agent add-preset codex-cli --id codex-read
agent-relay agent add-preset codex-app-server --id codex-app-read
agent-relay agent add-preset claude-code --id claude-read
agent-relay agent add-preset gemini-cli --id gemini-read
agent-relay agent add-preset antigravity-cli --id antigravity-read
agent-relay agent add-preset github-copilot --id copilot-read

agent-relay agent list
```

Run only the commands for installed providers. To register two Codex or Claude
accounts, authenticate two isolated configuration directories and give each
registration a distinct agent ID:

```bash
mkdir -p "$HOME/.agent-homes"

CODEX_HOME="$HOME/.agent-homes/codex-primary" codex login
CODEX_HOME="$HOME/.agent-homes/codex-backup" codex login
CLAUDE_CONFIG_DIR="$HOME/.agent-homes/claude-primary" claude auth login
CLAUDE_CONFIG_DIR="$HOME/.agent-homes/claude-backup" claude auth login

agent-relay agent add-preset codex-cli \
  --id codex-primary --config-home "$HOME/.agent-homes/codex-primary"
agent-relay agent add-preset codex-cli \
  --id codex-backup --config-home "$HOME/.agent-homes/codex-backup"
agent-relay agent add-preset claude-code \
  --id claude-primary --config-home "$HOME/.agent-homes/claude-primary"
agent-relay agent add-preset claude-code \
  --id claude-backup --config-home "$HOME/.agent-homes/claude-backup"
```

Keep those directories outside repositories and never commit them.

## 4. Create a task and configure fallback

Create a checkpoint using one registered read agent:

```bash
agent-relay task create \
  --title "Continue implementation" \
  --goal "Finish the selected issue and preserve verified context" \
  --agent codex-read
```

Copy the returned `task_id`, then configure and preview an ordered route. Every route
entry must be a read/plan preset, and the first entry must be the active agent:

```bash
agent-relay route set TASK_ID \
  --agent codex-read \
  --agent claude-read \
  --agent copilot-read

agent-relay route show TASK_ID
agent-relay route run TASK_ID
agent-relay route run TASK_ID --execute --cwd "$(git rev-parse --show-toplevel)"
```

Relay advances only after a recognized quota/rate-limit failure or a process that
never started. Authentication failures, timeouts, overload, and unknown failures
block instead of falling back.

`route set` rejects Gemini and Antigravity registrations, including registrations
already present in an older stored route. To invoke one manually, preview and then
execute an explicit handoff inside an OS-enforced read-only environment:

```bash
agent-relay handoff TASK_ID gemini-read
agent-relay handoff TASK_ID gemini-read --execute --cwd "$(git rev-parse --show-toplevel)"
```

The same rule applies to `antigravity-read`. Its plan-mode label is not a hard
filesystem boundary.

## 5. Run an explicitly reviewed write

Supported write presets are `codex-cli-write`, `codex-app-server-write`, and
`claude-code-write`. Claude Code write mode requires Claude Code 2.1.248 or newer.
Gemini and Antigravity remain manual-only because their current settings model
does not give Relay a sufficiently predictable read-only or exact-root write
boundary.

Register a separate writer; do not replace the read registration:

```bash
agent-relay agent add-preset claude-code-write --id claude-writer
# Or choose one Codex writer:
agent-relay agent add-preset codex-cli-write --id codex-writer
agent-relay agent add-preset codex-app-server-write --id codex-app-writer
```

Authorize one writer for one task and the exact Git root:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
agent-relay workspace authorize TASK_ID claude-writer --root "$REPO_ROOT"
agent-relay handoff TASK_ID claude-writer
agent-relay handoff TASK_ID claude-writer --execute --cwd "$REPO_ROOT"
```

After any change, the task remains blocked until review. Use the `action_id` and
`revision` returned by the executed handoff:

```bash
agent-relay workspace review TASK_ID WRITE_ACTION_ID
git status --short
git diff
git diff --cached

agent-relay workspace accept TASK_ID WRITE_ACTION_ID \
  --expected-revision CHECKPOINT_REVISION \
  --cwd "$REPO_ROOT"
```

If execution times out, exits non-zero, or cannot be inspected, assume its effects
are ambiguous. Restore only the action’s changes with Git or an editor, preserve all
pre-existing work, then verify the complete pre-run snapshot:

```bash
agent-relay workspace verify-rollback TASK_ID WRITE_ACTION_ID \
  --expected-revision CHECKPOINT_REVISION \
  --cwd "$REPO_ROOT"
```

Relay never performs the rollback for you.

## 6. Routine operations

```bash
agent-relay task show TASK_ID
agent-relay health list
agent-relay health show AGENT_ID
agent-relay workspace revoke TASK_ID WRITER_AGENT_ID

git pull --ff-only
python -m pip install -e .
PYTHONPYCACHEPREFIX=/tmp/agent-relay-pycache python -m unittest discover -v
```

Before upgrading, finish or roll back every pending workspace review and commit or
otherwise protect repository work.

## Troubleshooting

- **Executable not found:** run the provider’s `--version` command in the same shell,
  fix `PATH`, then retry `agent-relay agent add-preset`.
- **Provider authentication failure:** run the provider directly and sign in again.
  Relay intentionally blocks; it does not switch accounts or providers automatically.
- **Copilot authentication or policy failure:** run `copilot login`, then start
  `copilot` and use `/user` to verify the selected account. Organization-provided
  Copilot also requires the Copilot CLI policy to be enabled by the organization.
- **Student benefit is missing:** GitHub Education approval and Copilot Student
  activation are separate. Revisit the Education benefits page; do not buy a plan
  solely to work around a benefit that is still being applied.
- **Claude rejects `--restricted`:** upgrade Claude Code to 2.1.248 or newer. Do not
  replace it with `--dangerously-skip-permissions`.
- **Root rejected:** `cd` to the repository and use the exact output of
  `git rev-parse --show-toplevel`; a nested directory is not accepted.
- **Task is blocked:** inspect `agent-relay task show TASK_ID`. Resolve ordinary
  unknown actions explicitly, or use the workspace review/rollback flow for writes.
- **State directory rejected:** the default is
  `~/Library/Application Support/agent-relay`. Ensure it, its `tasks` directory, and
  managed files are real local paths, not symlinks. It must also be outside and
  disjoint from any Git root authorized for workspace writes. Do not edit state JSON
  manually.
