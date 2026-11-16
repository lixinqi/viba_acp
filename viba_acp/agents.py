"""The agents this service can drive, by name: their commands and the env they need.

`the_command(name)` answers one:

- `claude` — the Claude Agent SDK's ACP adapter, `npx -y
  @agentclientprotocol/claude-agent-acp`. It spawns the Claude CLI itself, so
  `CLAUDE_CODE_EXECUTABLE` points at the working one, else the one on `PATH`;
  its endpoint and token come from this process's environment;
- `dsh` — DeepSeek Harness's own `acp` profile, `dsh acp`; `DSH_EXECUTABLE`
  names the CLI, else the one on `PATH`; it uses the credentials `~/.dsh`
  already has.

`the_command()` answers `(command, extra environment)`: the environment is
layered over this process's own when the agent is spawned, never printed.

Nothing here names a machine. The CLIs are looked up on `PATH`, an environment
variable overrides the lookup when one lives outside it, and credentials stay
in the environment that started this process.
"""

import os
import shutil

CLAUDE_ADAPTER = ("npx", "-y", "@agentclientprotocol/claude-agent-acp")


def names():
    return ("claude", "dsh")


def the_command(name):
    """(command, extra environment) for one agent."""
    if name == "claude":
        executable = find_claude()
        env = {"CLAUDE_CODE_EXECUTABLE": executable} if executable else {}
        return list(CLAUDE_ADAPTER), env
    if name == "dsh":
        executable = find_dsh()
        if not executable:
            raise ValueError("the dsh CLI is not on PATH; set DSH_EXECUTABLE to it")
        return [executable, "acp"], {}
    raise ValueError(f"unknown agent {name!r}; choose one of {', '.join(names())}")


def find_claude():
    """The Claude CLI: `CLAUDE_CODE_EXECUTABLE`, else the one on `PATH`."""
    return shutil.which(os.environ.get("CLAUDE_CODE_EXECUTABLE") or "claude")


def find_dsh():
    """The dsh CLI: `DSH_EXECUTABLE`, else the one on `PATH`."""
    return shutil.which(os.environ.get("DSH_EXECUTABLE") or "dsh")


def available(name):
    """Whether this agent can start on this machine, as far as a path can tell."""
    if name == "claude":
        return shutil.which("npx") is not None and find_claude() is not None
    if name == "dsh":
        return find_dsh() is not None
    return False
