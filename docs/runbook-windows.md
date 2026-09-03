# Agent Relay runbook: Windows with WSL2

Agent Relay currently supports Windows through Windows Subsystem for Linux 2 (WSL2),
not native PowerShell execution. Its hardened local state store depends on POSIX file
descriptors, advisory file locking, and descriptor-relative path operations that are
not available through native Windows Python. WSL2 preserves those safety properties
and also matches the official [Codex WSL guidance](https://learn.chatgpt.com/docs/windows/wsl).

Keep the Relay checkout and working repositories inside the WSL Linux filesystem,
such as `/home/your-name/code`, rather than under `/mnt/c`.

## 1. Install and verify WSL2

Open PowerShell as Administrator:

```powershell
wsl --install -d Ubuntu
```

Restart Windows if prompted. Then open PowerShell and verify version 2:

```powershell
wsl --update
wsl --list --verbose
```

If the Ubuntu row says version 1, convert it:

```powershell
wsl --set-version Ubuntu 2
```

Use the exact distribution name shown by `wsl --list --verbose` if it differs from
`Ubuntu`.

Microsoft’s current installation details are in the
[WSL installation guide](https://learn.microsoft.com/windows/wsl/install).

## 2. Install Agent Relay inside Ubuntu

All remaining shell commands in this runbook run in the Ubuntu/WSL terminal, not
PowerShell:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl

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

Activate the virtual environment again after opening a new WSL terminal:

```bash
cd "$HOME/code/agent-relay"
source .venv/bin/activate
```

## 3. Install and authenticate providers inside WSL

Provider installations and logins must also live inside WSL. A provider installed
only in native Windows is not the same executable or configuration seen by Relay.

Install Codex CLI using the official [Codex CLI setup](https://learn.chatgpt.com/docs/codex/cli):

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex
```

Choose a supported sign-in method on first launch. For other providers, follow their
Linux instructions inside WSL:

- [Claude Code setup](https://code.claude.com/docs/en/setup)
- [Gemini CLI installation](https://geminicli.com/docs/get-started/installation/)
- [Antigravity CLI overview](https://antigravity.google/docs/cli/)

Install GitHub Copilot CLI inside WSL with GitHub's Linux installer, then use the
OAuth device flow. If the browser opens on Windows, finish authorization there and
return to the WSL terminal:

```bash
curl -fsSL https://gh.io/copilot-install | bash
copilot login
```

GitHub Education eligibility is separate from Relay. Verified students must activate
Copilot Student from the
[GitHub Education benefits page](https://github.com/settings/education/benefits).
GitHub's [student setup guide](https://docs.github.com/en/copilot/how-tos/copilot-on-github/set-up-copilot/enable-copilot/set-up-for-students)
explains the separate verification and activation steps.

Verify each provider that you installed:

```bash
codex --version
claude --version
gemini --version
agy --help
copilot --version
agent-relay agent presets
```

Relay reuses these local logins and never stores provider token values.
The Copilot preset requires Copilot CLI 1.0.79 or newer because older releases use
different sandbox authentication and developer-tool-access keys.

Before adding Copilot to an automatic route in a WSL image, run its authenticated
containment fixture once. It consumes one real Copilot request and also verifies
that the Linux sandbox backend works on that WSL installation:

```bash
AGENT_RELAY_RUN_COPILOT_NATIVE_TESTS=1 \
  python3 -m unittest tests.test_copilot_native -v
```

Keep Copilot out of automatic routes if this check does not pass. See the
[Copilot containment boundary](copilot-containment.md), including the exception
for administrator-installed policy hooks. Native Windows support is tracked
separately; GitHub currently documents that its Windows local sandbox requires a
Windows Insiders build.

## 4. Register agents and a route

Register only installed providers. Codex CLI, Claude Code, and GitHub Copilot read
presets are eligible for automatic routes. Gemini and Antigravity are available
only for explicit handoffs because their headless plan modes do not enforce
repository read-only access:

```bash
agent-relay agent add-preset codex-cli --id codex-read
agent-relay agent add-preset codex-app-server --id codex-app-read
agent-relay agent add-preset claude-code --id claude-read
agent-relay agent add-preset gemini-cli --id gemini-read
agent-relay agent add-preset antigravity-cli --id antigravity-read
agent-relay agent add-preset github-copilot --id copilot-read
```

Create a task, copy its `task_id`, then preview and execute the fallback route:

```bash
agent-relay task create \
  --title "Continue implementation" \
  --goal "Finish the selected issue and preserve verified context" \
  --agent codex-read

agent-relay route set TASK_ID \
  --agent codex-read \
  --agent claude-read \
  --agent copilot-read

agent-relay route show TASK_ID
agent-relay route run TASK_ID
agent-relay route run TASK_ID --execute --cwd "$(git rev-parse --show-toplevel)"
```

`route set` accepts only the Codex CLI, Claude Code, and GitHub Copilot read
presets. Relay falls through only for a recognized quota/rate-limit result or a
process that did not start. Ambiguous and authentication failures block.

Gemini and Antigravity registrations, including registrations found in older
stored routes, are rejected before any automatic-route provider launches. To use
one manually, preview and explicitly execute the handoff inside an OS-enforced
read-only environment:

```bash
agent-relay handoff TASK_ID gemini-read
agent-relay handoff TASK_ID gemini-read --execute --cwd "$(git rev-parse --show-toplevel)"
```

The same rule applies to `antigravity-read`. Its plan-mode label is not a hard
filesystem boundary.

## 5. Run an explicitly reviewed write

Supported write presets are `codex-cli-write`, `codex-app-server-write`, and
`claude-code-write`. Claude Code write mode requires Claude Code 2.1.248 or newer.
Gemini and Antigravity stay manual-only until their native controls can guarantee
a read-only or exact-root boundary.

Register a writer separately from the read agent:

```bash
agent-relay agent add-preset claude-code-write --id claude-writer
# Or choose one Codex writer:
agent-relay agent add-preset codex-cli-write --id codex-writer
agent-relay agent add-preset codex-app-server-write --id codex-app-writer
```

Authorize and execute against the exact Git root:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
agent-relay workspace authorize TASK_ID claude-writer --root "$REPO_ROOT"
agent-relay handoff TASK_ID claude-writer
agent-relay handoff TASK_ID claude-writer --execute --cwd "$REPO_ROOT"
```

Review the returned write action before accepting it:

```bash
agent-relay workspace review TASK_ID WRITE_ACTION_ID
git status --short
git diff
git diff --cached

agent-relay workspace accept TASK_ID WRITE_ACTION_ID \
  --expected-revision CHECKPOINT_REVISION \
  --cwd "$REPO_ROOT"
```

For a timeout, non-zero exit, or failed post-run inspection, restore only the action’s
changes while preserving pre-existing work, then verify rollback:

```bash
agent-relay workspace verify-rollback TASK_ID WRITE_ACTION_ID \
  --expected-revision CHECKPOINT_REVISION \
  --cwd "$REPO_ROOT"
```

Relay never resets or deletes repository files itself.

## 6. WSL operations and troubleshooting

```bash
agent-relay task show TASK_ID
agent-relay health list
git status --short
PYTHONPYCACHEPREFIX=/tmp/agent-relay-pycache python -m unittest discover -v
```

- **`fcntl` or `dir_fd` import/operation errors:** Relay was launched with native
  Windows Python. Open Ubuntu/WSL2 and reinstall it there.
- **Slow Git or filesystem behavior:** move the repository from `/mnt/c/...` to
  `/home/YOUR_USER/code/...` inside WSL.
- **Provider not found:** install it inside WSL and confirm its `--version` output in
  the same shell. A Windows-side installation is not sufficient.
- **Browser login does not return:** copy the displayed authentication URL into your
  Windows browser, finish login, then return to the WSL terminal.
- **Copilot authentication or policy failure:** run `copilot login`, then start
  `copilot` and use `/user` to verify the account. Organization-provided Copilot also
  requires its administrator to enable the Copilot CLI policy.
- **Student benefit is missing:** GitHub Education approval and Copilot Student
  activation are separate. Revisit the Education benefits page; do not buy a plan
  solely to work around a benefit that is still being applied.
- **Root rejected:** use the exact Linux path from `git rev-parse --show-toplevel`,
  never a `C:\...` path.
- **State directory rejected:** inside WSL the default is
  `${XDG_STATE_HOME:-$HOME/.local/state}/agent-relay`. Keep it outside and disjoint
  from every Git root authorized for workspace writes, and do not replace managed
  paths with symlinks.
- **Claude rejects `--restricted`:** upgrade Claude Code to 2.1.248 or newer; never
  substitute a permission-bypass flag.
- **Codex App confusion:** Relay starts its own local `codex app-server` subprocess
  inside WSL. It does not control or scrape the native Codex desktop UI.

Do not expose App Server over a non-local WebSocket. Relay uses the documented local
stdio transport, and all handoffs remain explicit and auditable.
