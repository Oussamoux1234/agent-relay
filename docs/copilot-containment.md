# GitHub Copilot CLI containment boundary

Agent Relay 0.9.7 gives the `github-copilot` preset a dedicated adapter instead
of launching it through the generic CLI adapter. The profile is designed for
repository inspection and is intentionally narrower than an ordinary Copilot
session.

GitHub Copilot CLI 1.0.79 or newer is required. Local sandboxing is still a
GitHub public-preview feature, so this document describes the exact boundary and
the validation required before treating it as a security control on a host.

## Per-execution isolation

For every handoff, Relay creates a private temporary `COPILOT_HOME` and cache,
imports only the bounded authentication fields from a regular `config.json`, and
writes fresh settings. It never imports saved permissions, hooks, plugins, extensions,
MCP/LSP definitions, instructions, sessions, or logs from the user's normal
configuration directory. Symlinks and authentication files larger than 2 MiB are
not copied. The temporary directory is deleted after the process exits.

The adapter accepts only the reviewed preset command and rejects inherited
environment allowlists. The command:

- exposes only `view`, `glob`, and `grep`;
- explicitly denies write, create, edit, shell, PowerShell, URL, memory,
  subagent, and web-fetch tools;
- disables built-in MCP servers and custom instructions;
- disables remote control, remote export, automatic updates, temporary-directory
  grants, and sandbox bypass;
- enables the experimental local sandbox and requires it to be available.

The generated sandbox policy does not automatically grant the working directory
read/write access. It grants the canonical repository path read-only, grants no
read/write paths, disables developer-tool cache discovery, disables Git and `gh`
credential injection, blocks outbound and local network access for sandboxed
tools, and blocks macOS Keychain access from those tools.

Copilot itself still needs provider/authentication traffic to obtain a model
response. The network rule contains sandboxed tools and child processes; it is
not an offline mode for the parent Copilot process.

## Hook boundary

Relay sets `disableAllHooks` in its isolated user settings and leaves the isolated
home without user/plugin hook sources. It also sets
`GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS=false`, does not copy saved folder trust,
and forces `COPILOT_ALLOW_ALL=false`. This prevents an untrusted repository from
turning a programmatic handoff into a repository-hook execution path even when
the repository contains a conflicting `disableAllHooks: false` setting.

GitHub documents one unavoidable exception: administrator-installed policy hooks
are machine-wide and cannot be disabled by `disableAllHooks`. On Linux and macOS
they live under `/etc/github-copilot/policy.d`; on Windows they may also come from
the machine policy registry. Relay treats these hooks as trusted administrator
code. Do not use the Copilot route on a machine whose policy hooks you do not
trust, or place the entire Relay/Copilot process inside an independently managed
outer OS sandbox.

## Native verification

The default unit suite verifies the exact command, isolated settings, bounded
authentication-state copy, symlink rejection, environment isolation, and cleanup.
The authenticated native fixture makes one real Copilot request and must be run
on every supported host image before that image is approved:

```bash
AGENT_RELAY_RUN_COPILOT_NATIVE_TESTS=1 \
  python3 -m unittest tests.test_copilot_native -v
```

The fixture creates an in-repository token, a sibling secret, and a malicious
repository `sessionStart` hook that tries to write both inside and outside the
workspace. It passes only when Copilot reads the repository token, cannot disclose
the sibling secret, and executes neither hook write.

Until this native fixture passes on the exact Copilot version and operating-system
image in use, call the profile `sandbox-read-contained-preview`, not a universal
hard read-only guarantee. If the sandbox backend or policy cannot be enforced,
`sandbox.failIfUnavailable` makes the execution fail instead of silently running
without containment.

## References

- [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [GitHub Copilot CLI configuration directory](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)
- [Configuring local sandbox settings](https://docs.github.com/en/copilot/how-tos/cloud-and-local-sandboxes/configuring-local-sandbox-settings)
- [GitHub Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference)
