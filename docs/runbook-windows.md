# Agent Relay runbook: native Windows and WSL2

Agent Relay v0.10.0 supports native 64-bit Windows Python and PowerShell. WSL2
remains supported as an alternative. Native support is gated by the repository's
Windows CI matrix on every supported Python version (3.9 through 3.14).

## Native Windows safety boundary

Relay uses a Windows-specific backend rather than emulating its POSIX controls:

- state defaults to `%LOCALAPPDATA%\agent-relay`;
- state must live on a local fixed NTFS or ReFS volume;
- the state root, `tasks` directory, lock, and JSON files reject symbolic links,
  junctions, and other reparse points;
- every mutation holds an exclusive native byte-range lock across the complete
  read-modify-write transaction;
- JSON is flushed and replaced from a unique file in the same directory, with
  directory and file identities checked before and after the operation;
- providers start behind a one-byte gate and are attached to a Windows Job Object
  before they can create children; timeout, `KeyboardInterrupt`, `SystemExit`, and
  normal teardown close or terminate the complete process tree;
- provider commands are argv arrays with `shell=False`. Relay rejects `.bat` and
  `.cmd` provider shims because Windows may parse them through a command shell.

These controls protect Relay's own state and lifecycle. A custom provider still
runs with the user's OS permissions. Provider read/write containment is supplied by
the reviewed preset and remains subject to the provider-specific boundary described
in the README.

## 1. Install native prerequisites

Open PowerShell. Install 64-bit Python 3.9 or newer and Git for Windows using your
normal organization-approved method. With Windows Package Manager, for example:

```powershell
winget install --id Python.Python.3.14 --exact
winget install --id Git.Git --exact
```

Close and reopen PowerShell, then verify:

```powershell
py -3 --version
git --version
```

## 2. Install Agent Relay natively

Keep the checkout and its virtual environment separate from Relay state:

```powershell
New-Item -ItemType Directory -Force "$HOME\code" | Out-Null
Set-Location "$HOME\code"
git clone https://github.com/Oussamoux1234/agent-relay.git
Set-Location agent-relay

py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .

agent-relay --help
agent-relay agent presets
```

In a new terminal, reactivate with:

```powershell
Set-Location "$HOME\code\agent-relay"
.\.venv\Scripts\Activate.ps1
```

The default state path is `$env:LOCALAPPDATA\agent-relay`. To choose another
location, use either `$env:AGENT_RELAY_STATE_DIR` or `--state-dir`. The replacement
must be outside every repository that may receive write authorization and must be on
a local fixed NTFS/ReFS volume:

```powershell
$env:AGENT_RELAY_STATE_DIR = "$env:LOCALAPPDATA\agent-relay-secondary"
agent-relay task list
```

Do not place state on a network share, removable drive, repository, OneDrive
placeholder, junction, or symbolic link.

## 3. Install and verify native provider executables

Follow each provider's current Windows instructions and authenticate with its own
CLI. Relay does not store provider token values.

- [OpenAI Codex CLI](https://learn.chatgpt.com/docs/codex/cli)
- [Anthropic Claude Code](https://code.claude.com/docs/en/setup)
- [Gemini CLI](https://geminicli.com/docs/get-started/installation/)
- [Antigravity CLI](https://antigravity.google/docs/cli/)
- [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli)

Inspect the resolved executable before registering a preset:

```powershell
Get-Command codex, claude, gemini, agy, copilot -ErrorAction SilentlyContinue |
  Select-Object Name, Source
```

A built-in preset needs a native executable such as `.exe`. If a provider is exposed
only through an npm `.cmd` shim, do not bypass Relay's rejection. Install the
provider's native executable or register the underlying executable and script as a
manual custom agent with separate `--arg` values. Custom registrations are never
eligible for automatic quota fallback.

GitHub Education eligibility is separate from Relay. Verified students must activate
Copilot Student from the
[GitHub Education benefits page](https://github.com/settings/education/benefits).

Copilot's Relay preset also requires Copilot CLI 1.0.79 or newer. GitHub currently
limits its native Windows local sandbox to supported Windows configurations; keep
Copilot out of automatic routes unless the authenticated containment fixture passes:

```powershell
$env:AGENT_RELAY_RUN_COPILOT_NATIVE_TESTS = "1"
python -m unittest tests.test_copilot_native -v
Remove-Item Env:AGENT_RELAY_RUN_COPILOT_NATIVE_TESTS
```

That fixture consumes one real Copilot request.

## 4. Register agents and run a route

Register only the installed native executables:

```powershell
agent-relay agent add-preset codex-cli --id codex-read
agent-relay agent add-preset codex-app-server --id codex-app-read
agent-relay agent add-preset claude-code --id claude-read
agent-relay agent add-preset github-copilot --id copilot-read
```

Codex CLI, Codex App Server, Claude Code, and Copilot reviewed read presets may be
used in an automatic route. Gemini and Antigravity remain explicit-handoff-only
because their current plan modes are not hard read-only boundaries.

```powershell
agent-relay task create `
  --title "Continue implementation" `
  --goal "Finish the selected issue and preserve verified context" `
  --agent codex-read

agent-relay route set TASK_ID `
  --agent codex-read `
  --agent claude-read `
  --agent copilot-read

$RepoRoot = (git rev-parse --show-toplevel).Trim()
agent-relay route show TASK_ID
agent-relay route run TASK_ID
agent-relay route run TASK_ID --execute --cwd $RepoRoot
```

Relay falls through only after a recognized quota/rate-limit failure or a provider
that did not start. Authentication, timeout, interruption, overload, and unknown
failures block instead of launching a second agent.

## 5. Run an explicitly reviewed write

Register one supported writer separately:

```powershell
agent-relay agent add-preset claude-code-write --id claude-writer
# Or use codex-cli-write / codex-app-server-write.
```

Authorize the exact Git root, preview, and execute:

```powershell
$RepoRoot = (git rev-parse --show-toplevel).Trim()
agent-relay workspace authorize TASK_ID claude-writer --root $RepoRoot
agent-relay handoff TASK_ID claude-writer
agent-relay handoff TASK_ID claude-writer --execute --cwd $RepoRoot
```

Review and accept only the exact recorded snapshot:

```powershell
agent-relay workspace review TASK_ID WRITE_ACTION_ID
git status --short
git diff
git diff --cached

agent-relay workspace accept TASK_ID WRITE_ACTION_ID `
  --expected-revision CHECKPOINT_REVISION `
  --cwd $RepoRoot
```

For a timeout, non-zero exit, interruption, or post-run inspection failure, restore
only that action's changes while preserving pre-existing work, then verify:

```powershell
agent-relay workspace verify-rollback TASK_ID WRITE_ACTION_ID `
  --expected-revision CHECKPOINT_REVISION `
  --cwd $RepoRoot
```

Relay never resets, deletes, or rolls back repository files itself.

## 6. Native diagnostics

```powershell
agent-relay task show TASK_ID
agent-relay health list
git status --short
python -m unittest discover -v
```

- **State volume rejected:** use the default under `LOCALAPPDATA` on a local fixed
  NTFS/ReFS drive. Network and removable state are intentionally unsupported.
- **Reparse point rejected:** replace the junction, symlink, or placeholder with a
  real local directory/file. Do not copy only part of Relay's state.
- **`.cmd` or `.bat` rejected:** select the provider's native `.exe`; Relay will not
  put checkpoint text through command-shell parsing.
- **Provider not found:** reopen PowerShell after installation and inspect
  `Get-Command PROVIDER | Select-Object Source`.
- **Task blocked after interruption:** inspect the recorded action. Relay terminated
  its Job Object, but a started provider is conservatively recorded as potentially
  effectful.
- **Codex App confusion:** Relay starts its own local `codex app-server` subprocess.
  It does not control or scrape the Codex desktop UI.

## 7. WSL2 alternative

Install WSL2 from an Administrator PowerShell and keep both checkout and workspaces
inside the Linux filesystem rather than `/mnt/c`:

```powershell
wsl --install -d Ubuntu
wsl --update
wsl --list --verbose
```

Then, inside Ubuntu:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl
mkdir -p "$HOME/code"
cd "$HOME/code"
git clone https://github.com/Oussamoux1234/agent-relay.git
cd agent-relay
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
agent-relay --help
```

Provider installations and logins must also exist inside WSL. The default state is
`${XDG_STATE_HOME:-$HOME/.local/state}/agent-relay`. Use Linux paths for `--cwd` and
`--root`. Microsoft maintains the current
[WSL installation guide](https://learn.microsoft.com/windows/wsl/install).
