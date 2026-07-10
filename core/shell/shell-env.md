# Shell & environment knowledge (zsh-first; bash only as a fallback)

Hard-won facts about how an agentic host gets its environment on Leo's machines. The standard
shell is zsh — fall back to bash only where zsh is unavailable. The notes below are written for
zsh; bash differences are called out. This is reference knowledge (not a deployed payload file).

## The inheritance model (all hosts, all shells)

Tool and hook commands inherit the environment of the process that launched the host (Claude Code,
Codex, OpenCode, Cursor). They do not re-source shell profiles per command. Consequences:

- The shell that launches the host supplies the harness's PATH/env — keep its login config correct
  (zsh: `~/.zprofile` / `~/.zshrc`; bash: `~/.bash_profile` / `~/.profile`). Nothing extra needed.
- Env changes made AFTER launch (e.g. editing `~/.zprofile`) are invisible until the host restarts.
- Diagnosis rule: a tool works in Leo's terminal but is missing in the agent → restart the host
  session/app after fixing PATH.
- Tool commands and hook wrappers emit POSIX syntax and run via `sh`. Parity with the login shell,
  not shell-swapping, is the goal.

## Hooks & absolute paths (self-locating, relocatable)

Hook scripts (`bash-guard.py`, `format-on-edit.py`, `council.py`) are **symlinked** from each tool
home into the leos-agent clone and **self-locate** the clone via `realpath(__file__)`, so they find
their machine-local config in `<clone>/local/` regardless of which tool home invoked them and even
if the home is relocated. Registrations invoke the symlinked `leos-python` launcher, which uses
only `<clone>/local/.venv/bin/python`; the guard wrapper blocks on a missing/broken launcher or
script, while formatter/council availability remains fail-open by design.

Where host config expects a literal command path (e.g. a notifier), render the absolute path from
THIS machine (`command -v <tool>`). Never copy one from another machine.

## Package-manager global bins

- **zsh** (standard) — add the global-bin dir in `~/.zprofile`:
  ```sh
  export PNPM_HOME="$HOME/Library/pnpm"
  export PATH="$PNPM_HOME/bin:$PNPM_HOME:$PATH"
  ```
  Adapt for yarn/npm global dirs per the machine's package-manager choice.
- **bash** (fallback only) — the same `export` lines in `~/.bash_profile` (or `~/.profile`).
- **pnpm gotcha**: `pnpm setup` inside a project directory may execute a project script named
  `setup` instead of pnpm's builtin. Run it from `$HOME`.

## macOS quirks

- `/var` is `/private/var`; temp dirs (`/var/folders`, `/private/tmp`) are legitimate workspaces
  (bash-guard exempts them).
- Homebrew on ARM lives at `/opt/homebrew/bin` — usually already on PATH via the launching shell;
  external reviewer CLIs installed there (`claude`, `opencode`, `codex`, `cursor-agent`) are safe to
  reference absolutely in seat argv if PATH proves unreliable on a machine.
